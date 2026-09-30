"""Capture a pregame forecast for the *current* week's games that haven't
kicked off yet, onto games.forecast_* - see db/schema.sql's comment on
those columns for why this overwrites in place rather than keeping
history: only the current week's SCHEDULED games are ever re-captured, so
whatever's there once a game goes live is the last forecast captured
before kickoff. The values themselves come from Pirate Weather's hourly
entry *for kickoff* (not `currently`), plus a forecast_window_* summary
of the first FORECAST_WINDOW_HOURS of the game - or the kickoff day's
daily entry if kickoff is still past the hourly horizon
(games.forecast_source says which) - see
loader_helper.capture_pregame_forecast(). In-game/postgame conditions are
game_snapshots' job instead (src/loaders/game_snapshots_loader.py).

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

from config.config import configure_logging, load_env
from db.clients import get_d1
from src.loaders.loader_helper import capture_pregame_forecast, sql_batch_call

logger = logging.getLogger(__name__)

# Alerts are filtered against [game_time, game_time + this] - a rough upper
# bound on how long a game actually runs, so an alert that's only active
# well before or after the game doesn't get attached to its forecast (see
# loader_helper.capture_pregame_forecast()'s docstring).
GAME_DURATION_HOURS = 4

# The forecast_window_* summary (max precip chance/gusts, temp range, snow)
# covers [game_time, game_time + this] - deliberately shorter than
# GAME_DURATION_HOURS: weather through the first 3 hours affects most of
# the game, while the last hour barely matters since the game is ending.
FORECAST_WINDOW_HOURS = 3

_UPDATE_FORECAST_SQL = """
UPDATE games SET
    forecast_temp_f = ?, forecast_feels_like_f = ?, forecast_condition = ?, forecast_icon = ?,
    forecast_precip_type = ?, forecast_wind_speed_mph = ?, forecast_wind_gust_mph = ?,
    forecast_wind_direction = ?, forecast_precipitation_pct = ?, forecast_visibility_mi = ?,
    forecast_alerts_json = ?, forecast_window_precip_pct_max = ?, forecast_window_precip_type = ?,
    forecast_window_wind_gust_mph_max = ?, forecast_window_temp_f_low = ?,
    forecast_window_temp_f_high = ?, forecast_window_snow_accum_in = ?,
    forecast_hours_json = ?, forecast_source = ?, forecast_captured_at = ?
WHERE game_id = ?
"""


def load_pregame_weather() -> None:
    """capture a forecast for the current week's not-yet-started games with
    a known stadium"""
    client = get_d1()

    upcoming_games = client.query(
        "SELECT g.game_id, g.game_time, s.latitude, s.longitude, s.roof_type "
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
        game_time = datetime.strptime(row["game_time"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        source, kickoff, window = capture_pregame_forecast(
            row["latitude"],
            row["longitude"],
            row["roof_type"],
            f"game_id={row['game_id']}",
            kickoff=game_time,
            window_hours=FORECAST_WINDOW_HOURS,
            alert_window_hours=GAME_DURATION_HOURS,
        )
        if source is None:
            # enclosed stadium, fetch failed/came back empty, or kickoff is
            # past even the daily forecast horizon
            skipped += 1
            continue
        statements.append(
            (
                _UPDATE_FORECAST_SQL,
                [*kickoff, *window, source, captured_at, row["game_id"]],
            )
        )

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
