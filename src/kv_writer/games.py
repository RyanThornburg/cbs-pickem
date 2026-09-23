"""week:{season}:{weekNN}:games - see src/CLAUDE.md's KV writer section."""

import logging
from collections import defaultdict
from typing import Any

from config.config import SEASON, get_d1_config, get_kv_config
from db.d1_client import D1Client
from db.kv_client import KVClient
from src.kv_writer.shared import GAMES_SQL, PICKS_SQL, now_iso, resolve_current_week

logger = logging.getLogger(__name__)

# A game.status thats "live"
_LIVE_STATUSES = ("IN_PROGRESS", "HALFTIME", "DELAYED")

_SNAPSHOTS_SQL = """
SELECT gs.*
FROM game_snapshots gs
JOIN games g ON g.game_id = gs.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
ORDER BY gs.captured_at ASC
"""

_INCOMPLETE_WEEKS_SQL = (
    "SELECT week_number FROM weeks WHERE season_id = ? AND is_complete = 0"
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
        "weather_alert": snapshot["weather_alert"],
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
    the pregame forecast, frozen at whatever was last captured before
    kickoff - see the "live" block above for in-game/postgame conditions."""
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
        "weather_alert": game["forecast_alert"],
        "captured_at": game["forecast_captured_at"],
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


def write_week_games(week_number: int) -> None:
    """Write week:{season}:{weekNN}:games - one week's schedule, joined with
    who picked which side (naturally empty pre-lock - user_picks only ever
    has locked/revealed rows, see api/CLAUDE.md) and, for games currently in
    progress, the latest game_snapshots state."""
    d1 = D1Client(**get_d1_config())

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
                "record": _team_record(game, "home"),
            },
            "away_team": {
                "id": game["away_id"],
                "abbr": game["away_abbr"],
                "name": game["away_name"],
                "record": _team_record(game, "away"),
            },
            "status": game["status"],
            "status_desc": game["status_desc"],
            "home_score": game["home_score"],
            "away_score": game["away_score"],
            "game_time": game["game_time"],
            "cbs_spread": game["cbs_spread"],
            "tv_network": game["tv_network"],
            "gametracker_url": game["gametracker_url"],
            "stadium": _game_stadium(game),
            "forecast": _game_forecast(game),
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
            "updated_at": now_iso(),
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
    d1 = D1Client(**get_d1_config())
    current_week = resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s - not writing games key",
            SEASON,
        )
        return

    write_week_games(current_week)


def write_incomplete_weeks_games() -> None:
    """Write week:{season}:{weekNN}:games for every week that isn't fully
    FINAL yet - not just weeks.is_current.
    cbs can flip the current pool week before the previous weeks games finish.
    this helps clean up any stragglers"""
    d1 = D1Client(**get_d1_config())
    week_numbers = [
        row["week_number"] for row in d1.query(_INCOMPLETE_WEEKS_SQL, [SEASON]).results
    ]
    if not week_numbers:
        logger.info("No incomplete weeks for season %s - nothing to refresh", SEASON)
        return

    for week_number in week_numbers:
        write_week_games(week_number)
