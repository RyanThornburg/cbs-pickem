"""write json from db to cloudflare kv for web ui

Usage: uv run python -m src.kv_writer [local|prod]
"""

import logging
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from config.config import (
    FIRST_HALF_PAID_PLACES,
    OVERALL_PAID_PLACES,
    SEASON,
    SECOND_HALF_PAID_PLACES,
    SECOND_HALF_START_WEEK,
    configure_logging,
    get_d1_config,
    get_kv_config,
    load_env,
)
from db.d1_client import D1Client
from db.kv_client import KVClient

logger = logging.getLogger(__name__)

_PAID_PLACES = {
    "overall": OVERALL_PAID_PLACES,
    "first_half": FIRST_HALF_PAID_PLACES,
    "second_half": SECOND_HALF_PAID_PLACES,
}

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

# calculate vs adding a running total in db
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

# prior seasons count of a user from historical standings
_SEASONS_PLAYED_SQL = """
SELECT user_id, COUNT(DISTINCT season_id) AS prior_seasons
FROM historical_standings
GROUP BY user_id
"""

# filtering down sportsbooks to common ones
# not running a gambling site, so just return recognizable ones
_ODDS_BOOKMAKERS = ("draftkings", "fanduel", "betmgm", "betrivers", "bovada")

# trends thresholds - hand-picked judgment calls, not derived from data
_LONE_WOLF_MIN_OPPOSING = 3  # how big the other side must be for a solo pick to mean anything
_ONE_SIDED_MIN_PICKS = 3  # floor so an early, barely-revealed game can't look "lopsided"
_ONE_SIDED_THRESHOLD = 0.8  # consensus share needed to call a game one-sided
_LINE_MOVER_MIN_POINTS = 1.0  # spread/total movement below this isn't worth surfacing

_WEEK_CBS_SPREADS_SQL = """
SELECT g.game_id, g.cbs_spread
FROM games g
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
"""

# market is 'spread' or 'total' - home_point holds the Over line for 'total'
# (see odds_snapshots' own column comment in db/schema.sql)
_ODDS_MARKET_SQL = f"""
SELECT os.game_id, os.bookmaker, os.home_point, os.captured_at
FROM odds_snapshots os
JOIN games g ON g.game_id = os.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ? AND os.market = ?
  AND os.bookmaker IN ({",".join("?" * len(_ODDS_BOOKMAKERS))})
ORDER BY os.captured_at ASC
"""

_SEASON_GAMES_SQL = """
SELECT g.game_id, w.week_number, g.status, g.home_score, g.away_score, g.cbs_spread,
       ht.team_id AS home_id, ht.abbreviation AS home_abbr, ht.nick_name AS home_name,
       at.team_id AS away_id, at.abbreviation AS away_abbr, at.nick_name AS away_name
FROM games g
JOIN teams ht ON ht.team_id = g.home_team_id
JOIN teams at ON at.team_id = g.away_team_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ?
ORDER BY g.game_time
"""

_SEASON_PICKS_SQL = """
SELECT up.game_id, up.user_id, u.name, up.picked_team_id
FROM user_picks up
JOIN users u ON u.user_id = up.user_id
JOIN games g ON g.game_id = up.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ?
"""

_ALL_TEAMS_SQL = "SELECT team_id, abbreviation AS abbr, nick_name AS name FROM teams"

# src/orchestration.py's polling cursors - lets this health check answer
# "when did each thing last actually run" without duplicating that logic.
_ORCHESTRATION_STATE_SQL = "SELECT key, value FROM orchestration_state"

_MAPPING_GAPS_TOTALS_SQL = """
SELECT COUNT(*) AS distinct_count, COALESCE(SUM(occurrences), 0) AS total_occurrences
FROM mapping_gaps
"""

_MAPPING_GAPS_RECENT_SQL = """
SELECT source, entity_type, raw_value, context, first_seen_at, last_seen_at, occurrences
FROM mapping_gaps
ORDER BY last_seen_at DESC
LIMIT 20
"""

_SYSTEM_EVENTS_TOTALS_SQL = """
SELECT COUNT(*) AS distinct_count, COALESCE(SUM(occurrences), 0) AS total_occurrences
FROM system_events
"""

_SYSTEM_EVENTS_RECENT_SQL = """
SELECT source, message, first_seen_at, last_seen_at, occurrences
FROM system_events
ORDER BY last_seen_at DESC
LIMIT 20
"""

# Deliberately more generous than orchestration.py's own
# ODDS_INTERVAL_SECONDS (6h)/HOUSEKEEPING_INTERVAL_SECONDS (24h) - a long
# live-heavy Sunday can legitimately delay the quiet-only branch these
# gate for hours without anything actually being wrong. Not imported from
# orchestration.py directly to avoid a circular import (orchestration.py
# already imports from this module); duplicated here as a deliberately
# looser, presentation-layer judgment call rather than the exact operational
# cadence.
_ODDS_STALE_SECONDS = 12 * 60 * 60
_HOUSEKEEPING_STALE_SECONDS = 48 * 60 * 60

# historical season records
_HISTORICAL_SQL = """
SELECT hs.season_id, s.name AS pool_name, s.historical_data_incomplete,
       u.user_id, u.name, u.is_active, hs.final_rank, hs.final_score,
       hs.first_half_rank, hs.first_half_score,
       hs.second_half_rank, hs.second_half_score
FROM historical_standings hs
JOIN users u ON u.user_id = hs.user_id
JOIN seasons s ON s.season_id = hs.season_id
ORDER BY hs.season_id, hs.final_rank
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
            "paid_places": _PAID_PLACES,
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


def _in_money(place: int | None, paid_places: int) -> bool:
    return place is not None and place <= paid_places


def _prior_seasons_by_user(d1: D1Client) -> dict[int, int]:
    return {
        row["user_id"]: row["prior_seasons"]
        for row in d1.query(_SEASONS_PLAYED_SQL).results
    }


def compute_week_leaderboard(
    d1: D1Client, week_number: int
) -> list[dict[str, Any]] | None:
    """
    overall and second half standings + current week
    returning ranked / ties
    using cbs status is_correct/trending_status/trending_score instead
    of calculating the actual results
    """
    performance_rows = d1.query(_WEEKLY_PERFORMANCE_SQL, [SEASON, week_number]).results
    if not performance_rows:
        logger.warning(
            "No weekly_performance found through season %s week %s",
            SEASON,
            week_number,
        )
        return None

    in_second_half = week_number >= SECOND_HALF_START_WEEK

    names: dict[int, str] = {}
    weekly_score: dict[int, int] = {}
    trending_score: dict[int, int] = {}
    cumulative_score: dict[int, int] = {}
    first_half_score: dict[int, int] = {}
    second_half_score: dict[int, int] = {}

    for row in performance_rows:
        user_id = row["user_id"]
        names[user_id] = row["name"]
        picks_correct = row["picks_correct"] or 0
        cumulative_score[user_id] = cumulative_score.get(user_id, 0) + picks_correct
        if row["week_number"] < SECOND_HALF_START_WEEK:
            first_half_score[user_id] = first_half_score.get(user_id, 0) + picks_correct
        elif in_second_half:
            second_half_score[user_id] = (
                second_half_score.get(user_id, 0) + picks_correct
            )
        if row["week_number"] == week_number:
            weekly_score[user_id] = picks_correct
            trending_score[user_id] = row["trending_score"] or 0

    place = _standard_rank(cumulative_score)
    first_half_place = _standard_rank(first_half_score)
    second_half_place = _standard_rank(second_half_score) if in_second_half else {}

    prior_seasons_by_user = _prior_seasons_by_user(d1)

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
            # +1 for the current season itself because historical records don't have currrent season
            "seasons_played": prior_seasons_by_user.get(user_id, 0) + 1,
            "weekly_score": weekly_score.get(user_id, 0),
            "trending_score": trending_score.get(user_id, 0),
            "cumulative_score": cumulative_score[user_id],
            "place": place[user_id],
            "first_half_score": first_half_score.get(user_id),
            "first_half_place": first_half_place.get(user_id),
            "second_half_score": second_half_score.get(user_id)
            if in_second_half
            else None,
            "second_half_place": second_half_place.get(user_id)
            if in_second_half
            else None,
            "in_money_overall": _in_money(place.get(user_id), OVERALL_PAID_PLACES),
            "in_money_first_half": _in_money(
                first_half_place.get(user_id), FIRST_HALF_PAID_PLACES
            ),
            "in_money_second_half": in_second_half
            and _in_money(second_half_place.get(user_id), SECOND_HALF_PAID_PLACES),
            "picks": picks_by_user.get(user_id, []),
        }
        for user_id in cumulative_score
    ]
    users_json.sort(key=lambda u: u["place"])
    return users_json


def write_week_leaderboard(week_number: int, env: str = "local") -> None:
    """Write week:{season}:{weekNN}:leaderboard from compute_week_leaderboard()."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())
    users_json = compute_week_leaderboard(d1, week_number)
    if users_json is None:
        logger.warning(
            "No weekly_performance for season %s week %s - not writing leaderboard key",
            SEASON,
            week_number,
        )
        return

    kv = KVClient(**get_kv_config())
    kv.write(
        f"week:{SEASON}:{week_number:02d}:leaderboard",
        {
            "week": week_number,
            "second_half_start_week": SECOND_HALF_START_WEEK,
            "paid_places": _PAID_PLACES,
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


def _consensus_line(values: list[float]) -> tuple[float, int]:
    """mode not average of most books spread, ties broken on median of tie values"""
    counts = Counter(values)
    max_count = max(counts.values())
    tied = sorted(value for value, count in counts.items() if count == max_count)
    return statistics.median(tied), max_count


def _open_close_consensus_by_game(
    d1: D1Client, week_number: int, market: str = "spread"
) -> dict[int, dict[str, Any]]:
    """Per game, this week's opening/closing consensus line (spread or
    total) across _ODDS_BOOKMAKERS - each book's own earliest/latest
    odds_snapshots row stands in for "opening"/"closing" (see
    db/CLAUDE.md's odds_snapshots note on why MIN/MAX over captured_at
    replaces dedicated columns). Shared by write_week_odds() (spread) and
    write_week_trends() (spread + total movers) so all three use the
    exact same consensus numbers."""
    opening_by_game_book: dict[tuple[int, str], dict[str, Any]] = {}
    closing_by_game_book: dict[tuple[int, str], dict[str, Any]] = {}
    for row in d1.query(
        _ODDS_MARKET_SQL, [SEASON, week_number, market, *_ODDS_BOOKMAKERS]
    ).results:
        key = (row["game_id"], row["bookmaker"])
        opening_by_game_book.setdefault(key, row)  # first-seen wins (ASC order)
        closing_by_game_book[key] = row  # last-seen wins

    opens_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for (game_id, _bookmaker), row in opening_by_game_book.items():
        opens_by_game[game_id].append(row)

    closes_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for (game_id, _bookmaker), row in closing_by_game_book.items():
        closes_by_game[game_id].append(row)

    consensus_by_game: dict[int, dict[str, Any]] = {}
    for game_id in set(opens_by_game) | set(closes_by_game):
        opens = opens_by_game.get(game_id)
        closes = closes_by_game.get(game_id)
        if not opens or not closes:
            continue
        open_line, open_agreement = _consensus_line([row["home_point"] for row in opens])
        close_line, close_agreement = _consensus_line(
            [row["home_point"] for row in closes]
        )
        consensus_by_game[game_id] = {
            "book_count": len(closes),
            "open": open_line,
            "open_agreement": open_agreement,
            "open_captured_at": min(row["captured_at"] for row in opens),
            "close": close_line,
            "close_agreement": close_agreement,
            "close_captured_at": max(row["captured_at"] for row in closes),
        }
    return consensus_by_game


def write_week_odds(week_number: int, env: str = "local") -> None:
    """Write week:{season}:{weekNN}:odds - each game's cbs_spread (what the
    pool is graded against) alongside an opening/closing consensus line."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())

    games = d1.query(_WEEK_CBS_SPREADS_SQL, [SEASON, week_number]).results
    if not games:
        logger.warning(
            "No games found for season %s week %s - not writing odds key",
            SEASON,
            week_number,
        )
        return

    consensus_by_game = _open_close_consensus_by_game(d1, week_number)

    games_json: list[dict[str, Any]] = [
        {
            "game_id": game["game_id"],
            "cbs_spread": game["cbs_spread"],
            "market_spread": consensus_by_game.get(game["game_id"]),
        }
        for game in games
    ]

    kv = KVClient(**get_kv_config())
    kv.write(
        f"week:{SEASON}:{week_number:02d}:odds",
        {
            "week": week_number,
            "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "games": games_json,
        },
    )
    logger.info(
        "Wrote week:%s:%02d:odds (%d games) to KV (%s)",
        SEASON,
        week_number,
        len(games_json),
        env,
    )


def _game_team_dicts(game: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {"id": game["home_id"], "abbr": game["home_abbr"], "name": game["home_name"]},
        {"id": game["away_id"], "abbr": game["away_abbr"], "name": game["away_name"]},
    )


def _split_home_away(
    game: dict[str, Any], picks: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    home = [p for p in picks if p["picked_team_id"] == game["home_id"]]
    away = [p for p in picks if p["picked_team_id"] == game["away_id"]]
    return home, away


def _one_sided_entry(
    game: dict[str, Any],
    home_picks: list[dict[str, Any]],
    away_picks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    total = len(home_picks) + len(away_picks)
    if total < _ONE_SIDED_MIN_PICKS:
        return None

    home_pct = len(home_picks) / total
    away_pct = len(away_picks) / total
    if home_pct >= _ONE_SIDED_THRESHOLD:
        side, pct = "home", home_pct
    elif away_pct >= _ONE_SIDED_THRESHOLD:
        side, pct = "away", away_pct
    else:
        return None

    home_team, away_team = _game_team_dicts(game)
    return {
        "game_id": game["game_id"],
        "home_team": home_team,
        "away_team": away_team,
        "home_picks": len(home_picks),
        "away_picks": len(away_picks),
        "consensus_side": side,
        "consensus_pct": round(pct, 3),
    }


def _lone_wolf_entries(
    game: dict[str, Any],
    home_picks: list[dict[str, Any]],
    away_picks: list[dict[str, Any]],
    week_number: int | None = None,
) -> list[dict[str, Any]]:
    """One user alone on a side while the other side has a real crowd
    (_LONE_WOLF_MIN_OPPOSING) - a 1-vs-1 split this early isn't a story,
    it's just the second person to pick yet."""
    entries: list[dict[str, Any]] = []
    for picks, opposing, team_id, abbr in (
        (home_picks, away_picks, game["home_id"], game["home_abbr"]),
        (away_picks, home_picks, game["away_id"], game["away_abbr"]),
    ):
        if len(picks) != 1 or len(opposing) < _LONE_WOLF_MIN_OPPOSING:
            continue
        entry = {
            "game_id": game["game_id"],
            "user_id": picks[0]["user_id"],
            "name": picks[0]["name"],
            "picked_team_id": team_id,
            "abbr": abbr,
            "opposing_count": len(opposing),
        }
        if week_number is not None:
            entry["week_number"] = week_number
        entries.append(entry)
    return entries


def _movers_from_consensus(
    consensus_by_game: dict[int, dict[str, Any]], game_lookup: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Games whose consensus line moved open->close by at least
    _LINE_MOVER_MIN_POINTS, sorted by movement magnitude - anything
    smaller is just normal noise, not a real move."""
    movers: list[dict[str, Any]] = []
    for game_id, consensus in consensus_by_game.items():
        movement = consensus["close"] - consensus["open"]
        if abs(movement) < _LINE_MOVER_MIN_POINTS:
            continue
        home_team, away_team = _game_team_dicts(game_lookup[game_id])
        movers.append(
            {
                "game_id": game_id,
                "home_team": home_team,
                "away_team": away_team,
                "open": consensus["open"],
                "close": consensus["close"],
                "movement": round(movement, 1),
                "book_count": consensus["book_count"],
            }
        )
    movers.sort(key=lambda e: -abs(e["movement"]))
    return movers


def write_week_trends(week_number: int, env: str = "local") -> None:
    """Write week:{season}:{weekNN}:trends - pick popularity/cold teams,
    lopsided games, lone-wolf picks (one user alone on a side against a
    real crowd on the other), and the week's biggest spread movers.
    Popularity/cold-team splits only count a game once its picks are
    revealed (a game with zero total picks yet is unlocked, not actually
    cold - same ambiguity write_week_games() already documents)."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())

    games = d1.query(_GAMES_SQL, [SEASON, week_number]).results
    if not games:
        logger.warning(
            "No games found for season %s week %s - not writing trends key",
            SEASON,
            week_number,
        )
        return

    picks_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in d1.query(_PICKS_SQL, [SEASON, week_number]).results:
        picks_by_game[pick["game_id"]].append(pick)

    pick_popularity: list[dict[str, Any]] = []
    cold_teams: list[dict[str, Any]] = []
    one_sided_games: list[dict[str, Any]] = []
    lone_wolves: list[dict[str, Any]] = []

    for game in games:
        home_picks, away_picks = _split_home_away(
            game, picks_by_game.get(game["game_id"], [])
        )
        total = len(home_picks) + len(away_picks)
        if total == 0:
            continue  # not revealed/locked yet - can't call this "cold"

        home_team, away_team = _game_team_dicts(game)
        for team, picks, opponent_count in (
            (home_team, home_picks, len(away_picks)),
            (away_team, away_picks, len(home_picks)),
        ):
            if picks:
                pick_popularity.append(
                    {
                        **team,
                        "game_id": game["game_id"],
                        "pick_count": len(picks),
                        "opponent_pick_count": opponent_count,
                        "pct_of_game_pickers": round(len(picks) / total, 3),
                    }
                )
            else:
                cold_teams.append({**team, "game_id": game["game_id"]})

        one_sided = _one_sided_entry(game, home_picks, away_picks)
        if one_sided:
            one_sided_games.append(one_sided)

        lone_wolves.extend(_lone_wolf_entries(game, home_picks, away_picks))

    pick_popularity.sort(key=lambda e: -e["pick_count"])
    one_sided_games.sort(key=lambda e: -e["consensus_pct"])

    game_lookup = {game["game_id"]: game for game in games}
    spread_movers = _movers_from_consensus(
        _open_close_consensus_by_game(d1, week_number, market="spread"), game_lookup
    )
    total_movers = _movers_from_consensus(
        _open_close_consensus_by_game(d1, week_number, market="total"), game_lookup
    )

    kv = KVClient(**get_kv_config())
    kv.write(
        f"week:{SEASON}:{week_number:02d}:trends",
        {
            "week": week_number,
            "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pick_popularity": pick_popularity,
            "cold_teams": cold_teams,
            "one_sided_games": one_sided_games,
            "lone_wolves": lone_wolves,
            "spread_movers": spread_movers,
            "total_movers": total_movers,
        },
    )
    logger.info(
        "Wrote week:%s:%02d:trends (%d popular, %d cold, %d one-sided, "
        "%d lone wolves, %d spread movers, %d total movers) to KV (%s)",
        SEASON,
        week_number,
        len(pick_popularity),
        len(cold_teams),
        len(one_sided_games),
        len(lone_wolves),
        len(spread_movers),
        len(total_movers),
        env,
    )


def write_current_week_trends(env: str = "local") -> None:
    """Resolve weeks.is_current and write that week's trends key."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())
    current_week = _resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s (%s) - not writing trends key",
            SEASON,
            env,
        )
        return

    write_week_trends(current_week, env)


def _ats_side(game: dict[str, Any]) -> str | None:
    """Which side covered game['cbs_spread'] - None if the game isn't
    FINAL yet or is missing a spread/score. cbs_spread is the home team's
    line (negative = home favored); home covers when its actual margin
    beats that line."""
    if game["status"] != "FINAL":
        return None
    if game["cbs_spread"] is None or game["home_score"] is None or game["away_score"] is None:
        return None
    adjusted = game["home_score"] - game["away_score"] + game["cbs_spread"]
    if adjusted > 0:
        return "home"
    if adjusted < 0:
        return "away"
    return "push"


def write_season_trends(env: str = "local") -> None:
    """Write season:{season}:trends - season-long pick popularity, ATS
    cover record per team (from cbs_spread + final scores, independent of
    who actually picked them - works even for a team nobody in the pool
    ever picks), cold teams, and every lone-wolf pick logged this season."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())

    games = d1.query(_SEASON_GAMES_SQL, [SEASON]).results
    if not games:
        logger.warning(
            "No games found for season %s - not writing season trends key", SEASON
        )
        return

    all_teams = {
        row["team_id"]: {"id": row["team_id"], "abbr": row["abbr"], "name": row["name"]}
        for row in d1.query(_ALL_TEAMS_SQL).results
    }

    picks_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    pick_counts: Counter[int] = Counter()
    for pick in d1.query(_SEASON_PICKS_SQL, [SEASON]).results:
        picks_by_game[pick["game_id"]].append(pick)
        pick_counts[pick["picked_team_id"]] += 1

    total_picks = sum(pick_counts.values())
    team_pick_totals = [
        {
            **all_teams[team_id],
            "total_picks": count,
            "pct_of_all_picks": round(count / total_picks, 3) if total_picks else 0.0,
        }
        for team_id, count in pick_counts.items()
        if team_id in all_teams
    ]
    team_pick_totals.sort(key=lambda e: -e["total_picks"])

    cold_teams_season = [
        team for team_id, team in all_teams.items() if pick_counts.get(team_id, 0) == 0
    ]

    covers: Counter[int] = Counter()
    pushes: Counter[int] = Counter()
    losses: Counter[int] = Counter()
    for game in games:
        side = _ats_side(game)
        if side is None:
            continue
        if side == "push":
            pushes[game["home_id"]] += 1
            pushes[game["away_id"]] += 1
        elif side == "home":
            covers[game["home_id"]] += 1
            losses[game["away_id"]] += 1
        else:
            covers[game["away_id"]] += 1
            losses[game["home_id"]] += 1

    team_ats_record = []
    for team_id, team in all_teams.items():
        c, p, l = covers.get(team_id, 0), pushes.get(team_id, 0), losses.get(team_id, 0)
        if c + p + l == 0:
            continue
        decided = c + l
        team_ats_record.append(
            {
                **team,
                "covers": c,
                "pushes": p,
                "losses": l,
                "cover_pct": round(c / decided, 3) if decided else None,
            }
        )
    team_ats_record.sort(key=lambda e: (-(e["cover_pct"] or 0), -e["covers"]))

    lone_wolves: list[dict[str, Any]] = []
    for game in games:
        home_picks, away_picks = _split_home_away(
            game, picks_by_game.get(game["game_id"], [])
        )
        lone_wolves.extend(
            _lone_wolf_entries(game, home_picks, away_picks, week_number=game["week_number"])
        )

    kv = KVClient(**get_kv_config())
    kv.write(
        f"season:{SEASON}:trends",
        {
            "season": SEASON,
            "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "team_pick_totals": team_pick_totals,
            "cold_teams_season": cold_teams_season,
            "team_ats_record": team_ats_record,
            "lone_wolves_season": lone_wolves,
        },
    )
    logger.info(
        "Wrote season:%s:trends (%d teams picked, %d cold, %d ATS records, "
        "%d lone wolves) to KV (%s)",
        SEASON,
        len(team_pick_totals),
        len(cold_teams_season),
        len(team_ats_record),
        len(lone_wolves),
        env,
    )


def write_current_week_odds(env: str = "local") -> None:
    """Resolve weeks.is_current and write that week's odds key."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())
    current_week = _resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s (%s) - not writing odds key",
            SEASON,
            env,
        )
        return

    write_week_odds(current_week, env)


def write_historical(env: str = "local") -> None:
    """Write meta:historical - past champions and each user's all-time
    record, from historical_standings (backfilled once from the pre-2026
    archive, and going forward one row per user per season at close-out).
    Static/manual cadence - nothing changes here until a season closes."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())
    rows = d1.query(_HISTORICAL_SQL).results
    if not rows:
        logger.warning(
            "No historical_standings rows found - not writing meta:historical"
        )
        return

    years: dict[str, dict[str, Any]] = {}
    champions_by_season: dict[int, dict[str, Any]] = {}
    first_half_champions_by_season: dict[int, dict[str, Any]] = {}
    second_half_champions_by_season: dict[int, dict[str, Any]] = {}
    career: dict[int, dict[str, Any]] = {}

    for row in rows:
        season_key = str(row["season_id"])
        year = years.setdefault(
            season_key,
            {
                "pool_name": row["pool_name"],
                "incomplete": bool(row["historical_data_incomplete"]),
                "standings": [],
            },
        )
        year["standings"].append(
            {
                "user_id": row["user_id"],
                "name": row["name"],
                "rank": row["final_rank"],
                "score": row["final_score"],
                "first_half_rank": row["first_half_rank"],
                "first_half_score": row["first_half_score"],
                "second_half_rank": row["second_half_rank"],
                "second_half_score": row["second_half_score"],
            }
        )

        champion = champions_by_season.setdefault(
            row["season_id"],
            {
                "year": row["season_id"],
                "incomplete": bool(row["historical_data_incomplete"]),
                "names": ["??? unknown/missing user"]
                if row["historical_data_incomplete"]
                else [],
                "score": None,
            },
        )
        if not champion["incomplete"] and row["final_rank"] == 1:
            champion["names"].append(row["name"])
            champion["score"] = row["final_score"]

        # I didn't track these or have the data
        # season_close_out.py starts writing these going forward.
        if row["first_half_rank"] == 1:
            first_half = first_half_champions_by_season.setdefault(
                row["season_id"], {"year": row["season_id"], "names": [], "score": None}
            )
            first_half["names"].append(row["name"])
            first_half["score"] = row["first_half_score"]
        if row["second_half_rank"] == 1:
            second_half = second_half_champions_by_season.setdefault(
                row["season_id"], {"year": row["season_id"], "names": [], "score": None}
            )
            second_half["names"].append(row["name"])
            second_half["score"] = row["second_half_score"]

        record = career.setdefault(
            row["user_id"],
            {
                "user_id": row["user_id"],
                "name": row["name"],
                "is_active": bool(row["is_active"]),
                "appearances": [],
                "titles": 0,
                "best_finish": None,
                "best_finish_years": [],
            },
        )
        record["appearances"].append(row["season_id"])
        if row["final_rank"] == 1:
            record["titles"] += 1
        if record["best_finish"] is None or row["final_rank"] < record["best_finish"]:
            record["best_finish"] = row["final_rank"]
            record["best_finish_years"] = [row["season_id"]]
        elif row["final_rank"] == record["best_finish"]:
            record["best_finish_years"].append(row["season_id"])

    champions = sorted(champions_by_season.values(), key=lambda c: c["year"])
    first_half_champions = sorted(
        first_half_champions_by_season.values(), key=lambda c: c["year"]
    )
    second_half_champions = sorted(
        second_half_champions_by_season.values(), key=lambda c: c["year"]
    )

    kv = KVClient(**get_kv_config())
    kv.write(
        "meta:historical",
        {
            "years": years,
            "champions": champions,
            "first_half_champions": first_half_champions,
            "second_half_champions": second_half_champions,
            "career": sorted(career.values(), key=lambda c: c["user_id"]),
        },
    )
    logger.info(
        "Wrote meta:historical (%d years, %d career entries) to KV (%s)",
        len(years),
        len(career),
        env,
    )


def _seconds_since(iso_value: str | None, now: datetime) -> float | None:
    """None if the key has never run, or isn't a plain ISO8601 UTC
    timestamp (deadline_last_synced_sunday stores a bare date, not one)."""
    if iso_value is None:
        return None
    try:
        last_at = datetime.strptime(iso_value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (now - last_at).total_seconds()


def write_admin_status(env: str = "local") -> None:
    """Write meta:admin - a health-check summary for an admin page: when
    each orchestration task last ran, and open mapping_gaps/system_events
    to review. Recomputed unconditionally every tick (see orchestration.py's
    run_tick()) since it's a handful of cheap local SELECTs and freshness
    matters most exactly when something just broke.

    Staleness is only flagged for odds/housekeeping - the tasks expected to
    run eventually regardless of live/quiet state. The four live-only
    pollers (sports_io/cbs live polls, game_snapshot, live_game_stats) just
    report their raw last-run timestamp with no stale flag: "should this
    have run" for those depends on live-window history, which isn't worth
    the complexity for a first pass - most weeks they simply won't have run
    recently because nothing's live, and that's correct, not a problem.
    """
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())
    now = datetime.now(UTC)

    state = {row["key"]: row["value"] for row in d1.query(_ORCHESTRATION_STATE_SQL).results}

    # Odds has two independent cursors (the flat baseline and the
    # pre-kickoff capture, see orchestration.py) - either one running
    # recently means odds data is fresh, so staleness compares against
    # whichever last ran more recently.
    odds_ages = [
        age
        for age in (
            _seconds_since(state.get("odds_last_call_at"), now),
            _seconds_since(state.get("odds_prekickoff_last_call_at"), now),
        )
        if age is not None
    ]
    odds_age = min(odds_ages) if odds_ages else None
    housekeeping_age = _seconds_since(state.get("housekeeping_last_run_at"), now)

    last_run: dict[str, Any] = {
        "odds": {
            "baseline_last_at": state.get("odds_last_call_at"),
            "prekickoff_last_at": state.get("odds_prekickoff_last_call_at"),
            "stale": odds_age is None or odds_age > _ODDS_STALE_SECONDS,
        },
        "housekeeping": {
            "last_at": state.get("housekeeping_last_run_at"),
            "stale": housekeeping_age is None or housekeeping_age > _HOUSEKEEPING_STALE_SECONDS,
        },
        "sports_io_live_poll": {"last_at": state.get("sports_io_live_last_poll_at")},
        "cbs_live_poll": {"last_at": state.get("cbs_live_last_poll_at")},
        "game_snapshot_capture": {"last_at": state.get("game_snapshot_last_capture_at")},
        "live_game_stats_capture": {"last_at": state.get("live_game_stats_last_capture_at")},
        "deadline_last_synced_sunday": state.get("deadline_last_synced_sunday"),
    }

    mapping_gaps_totals = d1.query(_MAPPING_GAPS_TOTALS_SQL).results[0]
    system_events_totals = d1.query(_SYSTEM_EVENTS_TOTALS_SQL).results[0]

    kv = KVClient(**get_kv_config())
    kv.write(
        "meta:admin",
        {
            "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_run": last_run,
            "mapping_gaps": {
                "distinct_count": mapping_gaps_totals["distinct_count"],
                "total_occurrences": mapping_gaps_totals["total_occurrences"],
                "recent": d1.query(_MAPPING_GAPS_RECENT_SQL).results,
            },
            "system_events": {
                "distinct_count": system_events_totals["distinct_count"],
                "total_occurrences": system_events_totals["total_occurrences"],
                "recent": d1.query(_SYSTEM_EVENTS_RECENT_SQL).results,
            },
        },
    )
    logger.info(
        "Wrote meta:admin (%d mapping gaps, %d system events) to KV (%s)",
        mapping_gaps_totals["distinct_count"],
        system_events_totals["distinct_count"],
        env,
    )


def main(env: str = "local") -> None:
    "write data to kv"
    write_meta_current(env)
    write_current_week_games(env)
    write_current_week_leaderboard(env)
    write_current_week_odds(env)
    write_current_week_trends(env)
    write_season_trends(env)
    write_historical(env)
    write_admin_status(env)


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
