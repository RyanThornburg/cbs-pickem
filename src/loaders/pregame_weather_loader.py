"""Capture a pregame forecast for the *current* week's games that haven't
kicked off yet, onto games.forecast_* - see db/schema.sql's comment on
those columns for why this overwrites in place rather than keeping
history: only the current week's SCHEDULED games are ever re-captured, so
whatever's there once a game goes live is effectively "the forecast at
kickoff". In-game/postgame conditions are game_snapshots' job instead
(src/loaders/game_snapshots_loader.py).

Scoped to weeks.is_current, not just status = 'SCHEDULED' - the full
season schedule is seeded ahead of time by housekeeping
(sports_io_loader.load_games_data()), so every future week's games sit at
SCHEDULED until actually played, not just the current week's. Filtering
on status alone would fetch a forecast for the entire rest of the
season on every run instead of just this week.

Usage: uv run python -m src.loaders.pregame_weather_loader [local|prod]
"""

import logging
import sys
from datetime import UTC, datetime
from typing import Any

from config.config import configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.loaders.loader_helper import capture_weather, sql_batch_call

logger = logging.getLogger(__name__)

_UPDATE_FORECAST_SQL = """
UPDATE games SET
    forecast_temp_f = ?, forecast_feels_like_f = ?, forecast_condition = ?,
    forecast_precip_type = ?, forecast_wind_speed_mph = ?, forecast_wind_gust_mph = ?,
    forecast_wind_direction = ?, forecast_precipitation_pct = ?, forecast_visibility_mi = ?,
    forecast_alert = ?, forecast_captured_at = ?
WHERE game_id = ?
"""


def load_pregame_weather() -> None:
    """capture a forecast for the current week's not-yet-started games with
    a known stadium"""
    client = D1Client(**get_d1_config())

    upcoming_games = client.query(
        "SELECT g.game_id, s.latitude, s.longitude, s.roof_type "
        "FROM games g "
        "JOIN stadiums s ON s.stadium_id = g.stadium_id "
        "JOIN weeks w ON w.week_id = g.week_id "
        "WHERE w.is_current = 1 AND g.status = 'SCHEDULED'"
    ).results
    if not upcoming_games:
        logger.info("No upcoming games this week to capture a forecast for")
        return

    captured_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    statements: list[tuple[str, list[Any] | None]] = []
    skipped = 0
    for row in upcoming_games:
        weather = capture_weather(
            row["latitude"], row["longitude"], row["roof_type"], f"game_id={row['game_id']}"
        )
        if all(v is None for v in weather):
            skipped += 1  # enclosed stadium, or the fetch failed/came back empty
            continue
        statements.append((_UPDATE_FORECAST_SQL, [*weather, captured_at, row["game_id"]]))

    if skipped:
        logger.info(
            "Skipped %d game(s) - enclosed stadium or forecast unavailable", skipped
        )
    if not statements:
        logger.info("No forecasts captured")
        return

    sql_batch_call(statements, client)
    logger.info("Captured %d pregame forecast(s)", len(statements))


def main() -> None:
    """pregame weather forecasts"""
    load_pregame_weather()


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    main()
