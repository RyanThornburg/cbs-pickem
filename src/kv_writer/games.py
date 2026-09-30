"""week:{season}:{weekNN}:games - see src/CLAUDE.md's KV writer section."""

import json
import logging
from collections import defaultdict
from typing import Any

from config.config import SEASON
from db.clients import get_d1, get_kv
from src.kv_writer.game_details import player_line
from src.kv_writer.shared import (
    GAMES_SQL,
    PICKS_SQL,
    for_current_week,
    game_team_dicts,
    split_home_away,
)
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

# A game.status thats "live"
_LIVE_STATUSES = ("IN_PROGRESS", "HALFTIME", "DELAYED")

# latest snapshot per game only - snapshots are written up to every minute
# per live game, so reading the whole week's history each tick adds up fast
_LATEST_SNAPSHOTS_SQL = """
SELECT gs.*
FROM game_snapshots gs
WHERE gs.snapshot_id IN (
    SELECT MAX(gs2.snapshot_id)
    FROM game_snapshots gs2
    JOIN games g ON g.game_id = gs2.game_id
    JOIN weeks w ON w.week_id = g.week_id
    WHERE w.season_id = ? AND w.week_number = ?
    GROUP BY gs2.game_id
)
"""

_SCORING_PLAYS_SQL = """
SELECT p.game_id, p.quarter, p.clock, p.team_id, p.type, p.description,
    p.player_name, p.home_score, p.away_score
FROM game_scoring_plays p
JOIN games g ON g.game_id = p.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
ORDER BY p.game_id, p.sequence
"""

_LEADER_ROWS_SQL = """
SELECT gps.game_id, gps.team_id, gps.stat_group, gps.player_name,
    gps.sports_io_player_id, gps.player_image, gps.stats_json
FROM game_player_stats gps
JOIN games g ON g.game_id = gps.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
  AND gps.stat_group IN ('Passing', 'Rushing', 'Receiving')
"""

_INCOMPLETE_WEEKS_SQL = (
    "SELECT week_number FROM weeks WHERE season_id = ? AND is_complete = 0"
)

# incomplete weeks whose first game has kicked off, plus the current week
# even before its first kickoff (picks, forecasts) - the future weeks are
# left to the daily include_future refresh
_ACTIVE_INCOMPLETE_WEEKS_SQL = (
    _INCOMPLETE_WEEKS_SQL + " AND (start_time <= ? OR is_current = 1)"
)


def _snapshot_weather(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """None for domes/retractable roofs (weather columns stay null there) or
    a capture that predates a source ever answering, not just missing wind."""
    if snapshot["temperature_f"] is None:
        return None
    return {
        "temp_f": snapshot["temperature_f"],
        "feels_like_f": snapshot["feels_like_f"],
        "condition": snapshot["weather_condition"],
        "icon": snapshot["weather_icon"],
        "precip_type": snapshot["precip_type"],
        "wind_speed_mph": snapshot["wind_speed_mph"],
        "wind_gust_mph": snapshot["wind_gust_mph"],
        "precipitation_pct": snapshot["precipitation_pct"],
        "visibility_mi": snapshot["visibility_mi"],
        "weather_alerts": json.loads(snapshot["weather_alerts_json"] or "[]"),
    }


def _team_record(game: dict[str, Any], side: str) -> dict[str, Any] | None:
    """None if this team's record hasn't synced yet (teams_loader.py hasn't
    run, or this team's Sports IO standings row wasn't found)."""
    wins = game[f"{side}_wins"]
    if wins is None:
        return None
    return {
        "wins": wins,
        "losses": game[f"{side}_losses"],
        "ties": game[f"{side}_ties"],
    }


def _game_stadium(game: dict[str, Any]) -> dict[str, Any] | None:
    """None if this game has no resolved stadium yet."""
    if game["stadium_id"] is None:
        return None
    return {
        "name": game["stadium_name"],
        "city": game["stadium_city"],
        "state": game["stadium_state"],
        "country": game["stadium_country"],
        "latitude": game["stadium_latitude"],
        "longitude": game["stadium_longitude"],
        "roof_type": game["stadium_roof_type"],
        "surface_type": game["stadium_surface_type"],
    }


def _game_forecast(game: dict[str, Any]) -> dict[str, Any] | None:
    """None if no pregame capture has happened yet - enclosed stadiums
    (src/loaders/pregame_weather_loader.py skips them) never get one, and
    neither does a game whose forecast hasn't been captured yet. This is
    the pregame forecast for the kickoff hour, frozen at whatever was last
    captured before kickoff, plus `during_game` - a summary of the first
    few hours after kickoff (pregame_weather_loader.FORECAST_WINDOW_HOURS)
    so weather rolling in mid-game isn't missed. `source` is "daily" when
    kickoff was still past the hourly horizon - whole-day values, temp_f/
    feels_like_f null, during_game's temps the day's range, and
    during_game.hours empty (no hourly breakdown for a day-level forecast;
    also empty for a capture from before hours existed). See the "live"
    block above for in-game/postgame conditions."""
    if game["forecast_captured_at"] is None:
        return None
    return {
        "temp_f": game["forecast_temp_f"],
        "feels_like_f": game["forecast_feels_like_f"],
        "condition": game["forecast_condition"],
        "icon": game["forecast_icon"],
        "precip_type": game["forecast_precip_type"],
        "wind_speed_mph": game["forecast_wind_speed_mph"],
        "wind_gust_mph": game["forecast_wind_gust_mph"],
        "wind_direction": game["forecast_wind_direction"],
        "precipitation_pct": game["forecast_precipitation_pct"],
        "visibility_mi": game["forecast_visibility_mi"],
        "weather_alerts": json.loads(game["forecast_alerts_json"] or "[]"),
        "during_game": {
            "precipitation_pct_max": game["forecast_window_precip_pct_max"],
            "precip_type": game["forecast_window_precip_type"],
            "wind_gust_mph_max": game["forecast_window_wind_gust_mph_max"],
            "temp_f_low": game["forecast_window_temp_f_low"],
            "temp_f_high": game["forecast_window_temp_f_high"],
            "snow_accumulation_in": game["forecast_window_snow_accum_in"],
            "hours": json.loads(game["forecast_hours_json"] or "[]"),
        },
        "source": game["forecast_source"],
        "captured_at": game["forecast_captured_at"],
    }


def _game_linescore(game: dict[str, Any]) -> dict[str, Any] | None:
    """None before kickoff (every quarter column still null). Quarters not
    played yet stay null, ot stays null unless the game went to overtime."""
    sides = {
        side: {
            period: game[f"{side}_{period}_score"]
            for period in ("q1", "q2", "q3", "q4", "ot")
        }
        for side in ("home", "away")
    }
    if all(value is None for scores in sides.values() for value in scores.values()):
        return None
    return sides


def _game_leaders(
    game: dict[str, Any], rows_by_team: dict[int, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """Top passer/rusher/receiver per team by yards - None until player
    stats exist. The full player box score is in the per-game details
    key (game:{season}:{game_id}:details, src/kv_writer/game_details.py)."""
    if not rows_by_team:
        return None

    def leader(team_id: int, group: str) -> dict[str, Any] | None:
        lines = [
            player_line(row)
            for row in rows_by_team.get(team_id, [])
            if row["stat_group"] == group
        ]
        lines = [line for line in lines if isinstance(line["stats"].get("yards"), int)]
        return max(lines, key=lambda line: line["stats"]["yards"], default=None)

    return {
        side: {
            category: leader(game[f"{side}_id"], group)
            for category, group in (
                ("passing", "Passing"),
                ("rushing", "Rushing"),
                ("receiving", "Receiving"),
            )
        }
        for side in ("home", "away")
    }


def _snapshot_live_block(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "quarter": snapshot["quarter"],
        "time_remaining": snapshot["time_remaining"],
        "possession": snapshot["possession"],
        "down": snapshot["down"],
        "distance": snapshot["distance"],
        "down_distance_text": snapshot["down_distance_text"],
        # ESPN's yardLine: yards from the home team's goal line (0-100),
        # regardless of who has the ball. 0 is ESPN's "no spot" (e.g.
        # halftime), never a real ball position.
        "yard_line": snapshot["yard_line"] or None,
        "possession_text": snapshot["possession_text"],
        "is_red_zone": bool(snapshot["is_red_zone"]),
        "home_timeouts": snapshot["home_timeouts"],
        "away_timeouts": snapshot["away_timeouts"],
        "last_play": {
            "text": snapshot["last_play_text"],
            "type": snapshot["last_play_type"],
        },
        "drive_text": snapshot["drive_text"],
        # where the current drive started, same home-goal-line frame as
        # yard_line - ESPN reports it as state, so a missed poll can't
        # leave it wrong. Right after a change of possession it can still
        # be the previous drive's until the new drive's first snap.
        "drive_start": {
            "yard_line": snapshot["drive_start_yard_line"],
            "text": snapshot["drive_start_text"],
        },
        # ESPN, 0-100, as of last_play - null for a snapshot from before this
        # was captured or when ESPN had no situation
        "win_probability": {
            "home": snapshot["home_win_pct"],
            "away": snapshot["away_win_pct"],
        },
        "weather": _snapshot_weather(snapshot),
    }


def _prefer_snapshot_score(game_json: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """While a game is live, show the snapshot's score (ESPN, polled every
    15s by src/live_ticker.py) when it's ahead of games.home_score/
    away_score (Sports IO, polled once a minute). "Ahead" is a higher
    combined score - scores only go up, so this never shows an older
    snapshot over a newer Sports IO score (e.g. if ESPN stops answering).
    D1's games row itself stays Sports IO's."""
    home, away = snapshot["home_score"], snapshot["away_score"]
    if home is None or away is None:
        return
    current = (game_json["home_score"] or 0) + (game_json["away_score"] or 0)
    if home + away > current:
        game_json["home_score"] = home
        game_json["away_score"] = away


def write_week_games(week_number: int) -> None:
    """Write week:{season}:{weekNN}:games - one week's schedule, joined with
    who picked which side (naturally empty pre-lock - user_picks only ever
    has locked/revealed rows, see api/CLAUDE.md) and, for games currently in
    progress, the latest game_snapshots state."""
    d1 = get_d1()

    games = d1.query(GAMES_SQL, [SEASON, week_number]).results
    if not games:
        logger.warning(
            "No games found for season %s week %s - not writing games key",
            SEASON,
            week_number,
        )
        return

    picks_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in d1.query(PICKS_SQL, [SEASON, week_number]).results:
        picks_by_game[pick["game_id"]].append(pick)

    latest_snapshot_by_game = {
        snapshot["game_id"]: snapshot
        for snapshot in d1.query(_LATEST_SNAPSHOTS_SQL, [SEASON, week_number]).results
    }

    plays_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for play in d1.query(_SCORING_PLAYS_SQL, [SEASON, week_number]).results:
        plays_by_game[play["game_id"]].append(
            {k: v for k, v in play.items() if k != "game_id"}
        )

    leader_rows: defaultdict[int, defaultdict[int, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in d1.query(_LEADER_ROWS_SQL, [SEASON, week_number]).results:
        leader_rows[row["game_id"]][row["team_id"]].append(row)

    games_json: list[dict[str, Any]] = []
    for game in games:
        home_team, away_team = game_team_dicts(game)
        home_picks, away_picks = split_home_away(
            game, picks_by_game.get(game["game_id"], [])
        )
        game_json: dict[str, Any] = {
            "game_id": game["game_id"],
            "home_team": {**home_team, "record": _team_record(game, "home")},
            "away_team": {**away_team, "record": _team_record(game, "away")},
            "status": game["status"],
            "status_desc": game["status_desc"],
            "home_score": game["home_score"],
            "away_score": game["away_score"],
            "linescore": _game_linescore(game),
            "leaders": _game_leaders(game, leader_rows[game["game_id"]]),
            # chronological, score is after each play - [] until someone scores
            "scoring_plays": plays_by_game[game["game_id"]],
            "game_time": game["game_time"],
            "cbs_spread": game["cbs_spread"],
            "tv_network": game["tv_network"],
            "gametracker_url": game["gametracker_url"],
            "neutral_site": bool(game["neutral_site"]),
            "stadium": _game_stadium(game),
            "forecast": _game_forecast(game),
            "picks": {
                "home": [
                    {"user_id": p["user_id"], "name": p["name"]} for p in home_picks
                ],
                "away": [
                    {"user_id": p["user_id"], "name": p["name"]} for p in away_picks
                ],
            },
        }

        snapshot = latest_snapshot_by_game.get(game["game_id"])
        if snapshot and game["status"] in _LIVE_STATUSES:
            game_json["live"] = _snapshot_live_block(snapshot)
            _prefer_snapshot_score(game_json, snapshot)

        games_json.append(game_json)

    get_kv().write(
        f"week:{SEASON}:{week_number:02d}:games",
        {
            "week": week_number,
            "updated_at": utc_iso(),
            "games": games_json,
        },
    )
    logger.info(
        "Wrote week:%s:%02d:games (%d games) to KV",
        SEASON,
        week_number,
        len(games_json),
    )


def write_current_week_games() -> None:
    """Resolve weeks.is_current and write that week's games key."""
    for_current_week(write_week_games, "games key")


_WEEKS_FOR_GAMES_SQL = """
SELECT DISTINCT w.week_number
FROM games g
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND g.game_id IN ({})
"""


def write_games_weeks(game_ids: set[int]) -> None:
    """Rewrite the games key for just the weeks these games are in - what
    src/live_ticker.py calls after a snapshot changed, rather than every
    incomplete week."""
    if not game_ids:
        return
    d1 = get_d1()
    ids = sorted(game_ids)
    rows = d1.query(
        _WEEKS_FOR_GAMES_SQL.format(", ".join("?" * len(ids))), [SEASON, *ids]
    ).results
    for row in rows:
        write_week_games(row["week_number"])


def write_incomplete_weeks_games(include_future: bool = False) -> None:
    """Write week:{season}:{weekNN}:games for every week that isn't fully
    FINAL yet - not just weeks.is_current.
    cbs can flip the current pool week before the previous weeks games finish.
    this helps clean up any stragglers

    By default only weeks that have started (or are current) - this runs
    every tick, and rewriting all ~16 future weeks each minute was most of
    the pipeline's KV write volume for keys whose data (the schedule) only
    changes on the daily sync. include_future=True covers those too, for
    that daily refresh."""
    d1 = get_d1()
    if include_future:
        rows = d1.query(_INCOMPLETE_WEEKS_SQL, [SEASON]).results
    else:
        rows = d1.query(_ACTIVE_INCOMPLETE_WEEKS_SQL, [SEASON, utc_iso()]).results
    week_numbers = [row["week_number"] for row in rows]
    if not week_numbers:
        logger.info("No incomplete weeks for season %s - nothing to refresh", SEASON)
        return

    for week_number in week_numbers:
        write_week_games(week_number)
