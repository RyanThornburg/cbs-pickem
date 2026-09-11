"""Compute derived JSON blobs from D1 and write them to Cloudflare KV, for
cbs-pickem-web's Worker to read - D1 stays the system of record, KV is a
serving cache computed from it

Usage: uv run python -m src.kv_writer [local|prod]
"""

import logging
import sys
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from config.config import (
    SEASON,
    SECOND_HALF_START_WEEK,
    configure_logging,
    get_d1_config,
    get_kv_config,
    load_env,
)
from db.d1_client import D1Client
from db.kv_client import KVClient

logger = logging.getLogger(__name__)

# A game.status thats "live"
_LIVE_STATUSES = ("IN_PROGRESS", "HALFTIME")

_GAMES_SQL = """
SELECT g.game_id, g.status, g.home_score, g.away_score, g.game_time,
       g.cbs_spread, g.tv_network, g.gametracker_url,
       ht.team_id AS home_id, ht.abbreviation AS home_abbr, ht.nick_name AS home_name,
       at.team_id AS away_id, at.abbreviation AS away_abbr, at.nick_name AS away_name
FROM games g
JOIN teams ht ON ht.team_id = g.home_team_id
JOIN teams at ON at.team_id = g.away_team_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
ORDER BY g.game_time
"""

_PICKS_SQL = """
SELECT up.game_id, up.user_id, u.name, up.picked_team_id
FROM user_picks up
JOIN users u ON u.user_id = up.user_id
JOIN games g ON g.game_id = up.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
"""

_SNAPSHOTS_SQL = """
SELECT gs.*
FROM game_snapshots gs
JOIN games g ON g.game_id = gs.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
ORDER BY gs.captured_at ASC
"""

# Every week through the requested one, so cumulative/second-half sums can be
# computed here rather than needing a running total column somewhere in D1.
_WEEKLY_PERFORMANCE_SQL = """
SELECT wp.user_id, u.name, w.week_number, wp.picks_correct, wp.trending_score
FROM weekly_performance wp
JOIN weeks w ON w.week_id = wp.week_id
JOIN users u ON u.user_id = wp.user_id
WHERE w.season_id = ? AND w.week_number <= ?
"""

_LEADERBOARD_PICKS_SQL = """
SELECT up.user_id, up.game_id, up.picked_team_id, up.is_correct, up.trending_status
FROM user_picks up
JOIN games g ON g.game_id = up.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
"""


def _resolve_current_week(d1: D1Client) -> int | None:
    """weeks.is_current"""
    result = d1.query(
        "SELECT week_number FROM weeks WHERE season_id = ? AND is_current = 1",
        [SEASON],
    )
    return result.results[0]["week_number"] if result.results else None


def write_meta_current(env: str = "local") -> None:
    """Write meta:current - which week is live right now, for this season."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())
    current_week = _resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s (%s) - not writing meta:current",
            SEASON,
            env,
        )
        return

    kv = KVClient(**get_kv_config())
    kv.write(
        "meta:current",
        {
            "season": SEASON,
            "current_week": current_week,
            "second_half_start_week": SECOND_HALF_START_WEEK,
        },
    )
    logger.info("Wrote meta:current (week %d) to KV (%s)", current_week, env)


def _snapshot_weather(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """None for domes/retractable roofs (weather columns stay null there) or
    a capture that predates a source ever answering, not just missing wind."""
    if snapshot["temperature_f"] is None:
        return None
    return {
        "temp_f": snapshot["temperature_f"],
        "feels_like_f": snapshot["feels_like_f"],
        "condition": snapshot["weather_condition"],
        "precip_type": snapshot["precip_type"],
        "wind_speed_mph": snapshot["wind_speed_mph"],
        "wind_gust_mph": snapshot["wind_gust_mph"],
        "precipitation_pct": snapshot["precipitation_pct"],
        "visibility_mi": snapshot["visibility_mi"],
        "weather_alert": snapshot["weather_alert"],
    }


def _snapshot_live_block(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "quarter": snapshot["quarter"],
        "time_remaining": snapshot["time_remaining"],
        "possession": snapshot["possession"],
        "down": snapshot["down"],
        "distance": snapshot["distance"],
        "down_distance_text": snapshot["down_distance_text"],
        "is_red_zone": bool(snapshot["is_red_zone"]),
        "home_timeouts": snapshot["home_timeouts"],
        "away_timeouts": snapshot["away_timeouts"],
        "weather": _snapshot_weather(snapshot),
    }


def write_week_games(week_number: int, env: str = "local") -> None:
    """Write week:{season}:{weekNN}:games - one week's schedule, joined with
    who picked which side (naturally empty pre-lock - user_picks only ever
    has locked/revealed rows, see api/CLAUDE.md) and, for games currently in
    progress, the latest game_snapshots state."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())

    games = d1.query(_GAMES_SQL, [SEASON, week_number]).results
    if not games:
        logger.warning(
            "No games found for season %s week %s - not writing games key",
            SEASON,
            week_number,
        )
        return

    picks_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in d1.query(_PICKS_SQL, [SEASON, week_number]).results:
        picks_by_game[pick["game_id"]].append(pick)

    # last row per game_id wins - rows come back ordered by captured_at ASC
    latest_snapshot_by_game: dict[int, dict[str, Any]] = {}
    for snapshot in d1.query(_SNAPSHOTS_SQL, [SEASON, week_number]).results:
        latest_snapshot_by_game[snapshot["game_id"]] = snapshot

    games_json: list[dict[str, Any]] = []
    for game in games:
        game_picks = picks_by_game.get(game["game_id"], [])
        game_json: dict[str, Any] = {
            "game_id": game["game_id"],
            "home_team": {
                "id": game["home_id"],
                "abbr": game["home_abbr"],
                "name": game["home_name"],
            },
            "away_team": {
                "id": game["away_id"],
                "abbr": game["away_abbr"],
                "name": game["away_name"],
            },
            "status": game["status"],
            "home_score": game["home_score"],
            "away_score": game["away_score"],
            "game_time": game["game_time"],
            "cbs_spread": game["cbs_spread"],
            "tv_network": game["tv_network"],
            "gametracker_url": game["gametracker_url"],
            "picks": {
                "home": [
                    {"user_id": p["user_id"], "name": p["name"]}
                    for p in game_picks
                    if p["picked_team_id"] == game["home_id"]
                ],
                "away": [
                    {"user_id": p["user_id"], "name": p["name"]}
                    for p in game_picks
                    if p["picked_team_id"] == game["away_id"]
                ],
            },
        }

        snapshot = latest_snapshot_by_game.get(game["game_id"])
        if snapshot and game["status"] in _LIVE_STATUSES:
            game_json["live"] = _snapshot_live_block(snapshot)

        games_json.append(game_json)

    kv = KVClient(**get_kv_config())
    kv.write(
        f"week:{SEASON}:{week_number:02d}:games",
        {
            "week": week_number,
            "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "games": games_json,
        },
    )
    logger.info(
        "Wrote week:%s:%02d:games (%d games) to KV (%s)",
        SEASON,
        week_number,
        len(games_json),
        env,
    )


def write_current_week_games(env: str = "local") -> None:
    """Resolve weeks.is_current and write that week's games key - the call
    orchestration.py actually uses, since a live tick knows a game is live
    but not which week it belongs to without asking."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())
    current_week = _resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s (%s) - not writing games key",
            SEASON,
            env,
        )
        return

    write_week_games(current_week, env)


def _standard_rank(score_by_user: dict[int, int]) -> dict[int, int]:
    """highest first, ties cause next number to be skipped"""
    ranked = sorted(score_by_user.items(), key=lambda item: -item[1])
    rank_by_user: dict[int, int] = {}
    prev_score: int | None = None
    prev_rank = 0
    for i, (user_id, score) in enumerate(ranked, start=1):
        if score != prev_score:
            prev_rank = i
            prev_score = score
        rank_by_user[user_id] = prev_rank
    return rank_by_user


def write_week_leaderboard(week_number: int, env: str = "local") -> None:
    """Write week:{season}:{weekNN}:leaderboard - cumulative and second-half
    standings through this week, plus this week's picks. Ranking math
    (cumulative sums, tie-aware place) is computed here.
    No custom live-grading: is_correct/trending_status/trending_score are
    passed through exactly as CBS graded them."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())

    performance_rows = d1.query(_WEEKLY_PERFORMANCE_SQL, [SEASON, week_number]).results
    if not performance_rows:
        logger.warning(
            "No weekly_performance found through season %s week %s - "
            "not writing leaderboard key",
            SEASON,
            week_number,
        )
        return

    in_second_half = week_number >= SECOND_HALF_START_WEEK

    names: dict[int, str] = {}
    weekly_score: dict[int, int] = {}
    trending_score: dict[int, int] = {}
    cumulative_score: dict[int, int] = {}
    second_half_score: dict[int, int] = {}

    for row in performance_rows:
        user_id = row["user_id"]
        names[user_id] = row["name"]
        picks_correct = row["picks_correct"] or 0
        cumulative_score[user_id] = cumulative_score.get(user_id, 0) + picks_correct
        if in_second_half and row["week_number"] >= SECOND_HALF_START_WEEK:
            second_half_score[user_id] = (
                second_half_score.get(user_id, 0) + picks_correct
            )
        if row["week_number"] == week_number:
            weekly_score[user_id] = picks_correct
            trending_score[user_id] = row["trending_score"] or 0

    place = _standard_rank(cumulative_score)
    second_half_place = _standard_rank(second_half_score) if in_second_half else {}

    picks_by_user: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in d1.query(_LEADERBOARD_PICKS_SQL, [SEASON, week_number]).results:
        picks_by_user[pick["user_id"]].append(
            {
                "game_id": pick["game_id"],
                "team_id": pick["picked_team_id"],
                "is_correct": (
                    None if pick["is_correct"] is None else bool(pick["is_correct"])
                ),
                "trending_status": pick["trending_status"],
            }
        )

    users_json: list[dict[str, Any]] = [
        {
            "user_id": user_id,
            "name": names[user_id],
            "weekly_score": weekly_score.get(user_id, 0),
            "trending_score": trending_score.get(user_id, 0),
            "cumulative_score": cumulative_score[user_id],
            "place": place[user_id],
            "second_half_score": second_half_score.get(user_id)
            if in_second_half
            else None,
            "second_half_place": second_half_place.get(user_id)
            if in_second_half
            else None,
            "picks": picks_by_user.get(user_id, []),
        }
        for user_id in cumulative_score
    ]
    users_json.sort(key=lambda u: u["place"])

    kv = KVClient(**get_kv_config())
    kv.write(
        f"week:{SEASON}:{week_number:02d}:leaderboard",
        {
            "week": week_number,
            "second_half_start_week": SECOND_HALF_START_WEEK,
            "users": users_json,
        },
    )
    logger.info(
        "Wrote week:%s:%02d:leaderboard (%d users) to KV (%s)",
        SEASON,
        week_number,
        len(users_json),
        env,
    )


def write_current_week_leaderboard(env: str = "local") -> None:
    """Resolve weeks.is_current and write that week's leaderboard key."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())
    current_week = _resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s (%s) - not writing leaderboard key",
            SEASON,
            env,
        )
        return

    write_week_leaderboard(current_week, env)


def main(env: str = "local") -> None:
    "write data to kv"
    write_meta_current(env)
    write_current_week_games(env)
    write_current_week_leaderboard(env)


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
