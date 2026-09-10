"""
Capture a snapshot (score/quarter/possession + weather) for every currently-live game
CBS's pool-home page for game state
Pirate Weather for conditions at the stadium's location.

Usage: uv run python -m src.loaders.game_snapshots_loader [local|prod]
"""

import logging
import sys
from typing import Any

from api.cbs_client import get_cbs_pool_home
from api.espn_client import ABBREV_CORRECTIONS as ESPN_ABBREV_CORRECTIONS
from api.espn_client import get_scoreboard
from api.espn_models import Situation
from api.weather_api import get_forecast
from config.config import configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.loaders.loader_helper import mapping_gap_statement, sql_batch_call

logger = logging.getLogger(__name__)

_INSERT_SNAPSHOT_SQL = """
INSERT INTO game_snapshots (
    game_id, quarter, time_remaining, status_desc, possession, home_score, away_score,
    down, distance, yard_line, down_distance_text, possession_text,
    is_red_zone, home_timeouts, away_timeouts,
    temperature_f, feels_like_f, weather_condition, precip_type,
    wind_speed_mph, wind_gust_mph, wind_direction, precipitation_pct,
    visibility_mi, weather_alert
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# (quarter, time_remaining) identical don't update, nothing happened since prior
_LATEST_SNAPSHOT_SQL = """
SELECT game_id, quarter, time_remaining FROM game_snapshots
WHERE snapshot_id IN (SELECT MAX(snapshot_id) FROM game_snapshots GROUP BY game_id)
"""

_COMPASS_POINTS = [
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
]


def _bearing_to_compass(bearing: float) -> str:
    """Convert a wind bearing in degrees to a 16-point compass direction."""
    return _COMPASS_POINTS[round(bearing / 22.5) % 16]


_NO_SITUATION = (None, None, None, None, None, None, None, None)


def _situation_fields(
    situation: Situation | None,
) -> tuple[Any, Any, Any, Any, Any, Any, Any, Any]:
    """(down, distance, yard_line, down_distance_text, possession_text,
    is_red_zone, home_timeouts, away_timeouts). None across the board if
    ESPN has no situation for this game right now - e.g. between plays
    like halftime, or if ESPN's abbreviation-matched event wasn't found."""
    if situation is None:
        return _NO_SITUATION

    return (
        situation.down,
        situation.distance,
        situation.yard_line,
        situation.down_distance_text,
        situation.possession_text,
        situation.is_red_zone,
        situation.home_timeouts,
        situation.away_timeouts,
    )


_UPDATE_GAME_ESPN_ID_SQL = "UPDATE games SET espn_event_id = ? WHERE game_id = ?"


def _fetch_espn_scoreboard_lookup() -> tuple[
    dict[str, Situation | None], dict[tuple[str, str], tuple[str, Situation | None]]
]:
    """two lookups - espn event id falls or home/away if event id not yet added
    espn is an undocumented/extra data source so not blocking on failures from it"""
    try:
        scoreboard = get_scoreboard()
    except Exception:
        logger.exception("ESPN scoreboard fetch failed - skipping situation data")
        return {}, {}

    by_espn_id: dict[str, Situation | None] = {}
    by_teams: dict[tuple[str, str], tuple[str, Situation | None]] = {}
    for event in scoreboard.events:
        competition = event.competitions[0]
        by_espn_id[event.id] = competition.situation

        by_side = {c.home_away: c.team.abbreviation for c in competition.competitors}
        home = ESPN_ABBREV_CORRECTIONS.get(by_side.get("home", ""), by_side.get("home"))
        away = ESPN_ABBREV_CORRECTIONS.get(by_side.get("away", ""), by_side.get("away"))
        if home and away:
            by_teams[(home, away)] = (event.id, competition.situation)
    return by_espn_id, by_teams


_NO_WEATHER = (None, None, None, None, None, None, None, None, None, None)


def _weather_fields(
    latitude: float | None, longitude: float | None, game_id: int
) -> tuple[Any, Any, Any, Any, Any, Any, Any, Any, Any, Any]:
    """missing forecasts don't block"""
    if latitude is None or longitude is None:
        return _NO_WEATHER

    try:
        forecast = get_forecast(latitude, longitude)
    except Exception:
        logger.exception("Weather fetch failed for game_id=%s", game_id)
        return _NO_WEATHER

    current = forecast.currently
    if current is None:
        return _NO_WEATHER

    alert = "; ".join(a.title for a in forecast.alerts) or None

    return (
        round(current.temperature) if current.temperature is not None else None,
        round(current.apparent_temperature)
        if current.apparent_temperature is not None
        else None,
        current.summary,
        current.precip_type,
        round(current.wind_speed) if current.wind_speed is not None else None,
        round(current.wind_gust) if current.wind_gust is not None else None,
        _bearing_to_compass(current.wind_bearing)
        if current.wind_bearing is not None
        else None,
        round(current.precip_probability * 100)
        if current.precip_probability is not None
        else None,
        current.visibility,
        alert,
    )


def load_game_snapshots(env: str = "local") -> None:
    """capture a snapshot for every currently-live game"""
    if not load_env(env):
        sys.exit(1)

    client = D1Client(**get_d1_config())

    live_games = client.query(
        "SELECT g.game_id, g.cbs_event_id, g.espn_event_id, "
        "s.latitude, s.longitude, s.roof_type, "
        "ht.abbreviation AS home_abbrev, at.abbreviation AS away_abbrev "
        "FROM games g "
        "LEFT JOIN stadiums s ON s.stadium_id = g.stadium_id "
        "JOIN teams ht ON ht.team_id = g.home_team_id "
        "JOIN teams at ON at.team_id = g.away_team_id "
        "WHERE g.status IN ('IN_PROGRESS', 'HALFTIME')"
    ).results
    if not live_games:
        logger.info("No live games to snapshot")
        return

    data = get_cbs_pool_home()
    if data is None:
        return
    events_by_cbs_id = {e.cbs_event_id: e for e in data.pool_period.pool_events}

    espn_by_id, espn_by_teams = _fetch_espn_scoreboard_lookup()
    espn_backfill_statements: list[tuple[str, list[Any] | None]] = []

    latest_by_game_id = {
        row["game_id"]: (row["quarter"], row["time_remaining"])
        for row in client.query(_LATEST_SNAPSHOT_SQL).results
    }

    statements: list[tuple[str, list[Any] | None]] = []
    gap_statements: list[tuple[str, list[Any] | None]] = []
    skipped_unchanged = 0
    for row in live_games:
        event = events_by_cbs_id.get(row["cbs_event_id"])
        if event is None:
            logger.warning(
                "No CBS event for live game_id=%s (cbs_event_id=%s) - skipping snapshot",
                row["game_id"],
                row["cbs_event_id"],
            )
            continue

        if latest_by_game_id.get(row["game_id"]) == (
            event.game_period,
            event.time_remaining,
        ):
            skipped_unchanged += 1
            continue

        # Dome/Retractable stadiums are treated as always enclosed for weather purposes
        if row["roof_type"] in ("Dome", "Retractable"):
            weather = _NO_WEATHER
        else:
            weather = _weather_fields(row["latitude"], row["longitude"], row["game_id"])

        if row["espn_event_id"] is not None:
            situation = espn_by_id.get(row["espn_event_id"])
            if espn_by_id and row["espn_event_id"] not in espn_by_id:
                logger.warning(
                    "Linked ESPN event %s for game_id=%s not found in current "
                    "scoreboard - leaving situation unset",
                    row["espn_event_id"],
                    row["game_id"],
                )
        else:
            team_key = (row["home_abbrev"], row["away_abbrev"])
            match = espn_by_teams.get(team_key)
            if match is None:
                situation = None
                if espn_by_teams:
                    logger.warning(
                        "No ESPN event for live game_id=%s (%s @ %s) - leaving situation unset",
                        row["game_id"],
                        row["away_abbrev"],
                        row["home_abbrev"],
                    )
                    gap_statements.append(
                        mapping_gap_statement(
                            "espn",
                            "team_pair",
                            f"{row['away_abbrev']}@{row['home_abbrev']}",
                            "load_game_snapshots",
                        )
                    )
            else:
                espn_event_id, situation = match
                espn_backfill_statements.append(
                    (_UPDATE_GAME_ESPN_ID_SQL, [espn_event_id, row["game_id"]])
                )

        statements.append(
            (
                _INSERT_SNAPSHOT_SQL,
                [
                    row["game_id"],
                    event.game_period,
                    event.time_remaining,
                    event.game_status_desc,
                    event.possession if event.possession != "NONE" else None,
                    event.home_team_score,
                    event.away_team_score,
                    *_situation_fields(situation),
                    *weather,
                ],
            )
        )

    if espn_backfill_statements:
        sql_batch_call(espn_backfill_statements, client)

    if skipped_unchanged:
        logger.info(
            "Skipped %d snapshot(s) - clock unchanged since last capture",
            skipped_unchanged,
        )

    if not statements:
        if not skipped_unchanged:
            logger.warning("No snapshots captured")
        if gap_statements:
            sql_batch_call(gap_statements, client)
        return

    sql_batch_call(statements + gap_statements, client)
    logger.info("Captured %d game snapshots (%s)", len(statements), env)


def main(env: str = "local") -> None:
    """game snapshots"""
    load_game_snapshots(env)


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
