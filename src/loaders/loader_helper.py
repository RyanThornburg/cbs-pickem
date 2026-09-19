"""helper for src/loaders/"""

import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from api.weather_api import get_forecast
from api.weather_api_models import Alert
from config.config import get_d1_config
from db.d1_client import D1Client, D1Error

logger = logging.getLogger(__name__)

# Stadiums with these roof types are always treated as enclosed - no way to
# detect actual roof state (e.g. a retractable roof open on a nice day), so
# weather is deliberately skipped for both rather than guessed at.
ENCLOSED_ROOF_TYPES = ("Dome", "Retractable")

_NO_WEATHER = (None, None, None, None, None, None, None, None, None, None, None)

_COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]  # fmt: skip


def _bearing_to_compass(bearing: float) -> str:
    """Convert a wind bearing in degrees to a 16-point compass direction."""
    return _COMPASS_POINTS[round(bearing / 22.5) % 16]


def _alert_overlaps(alert: Alert, window_start: datetime, window_end: datetime) -> bool:
    """Pirate Weather returns whatever's active/upcoming for the location
    right now, regardless of the actual game - a Friday-only flood watch
    fetched while pregame-polling a Sunday game would otherwise get
    attached to that Sunday game's forecast even though it's long expired
    by kickoff. Overlap, not containment - an alert only needs to touch
    the window, not span all of it."""
    start = datetime.fromtimestamp(alert.time, tz=UTC)
    end = datetime.fromtimestamp(alert.expires, tz=UTC) if alert.expires else None
    if end is not None and end < window_start:
        return False
    return start <= window_end


def capture_weather(
    latitude: float | None,
    longitude: float | None,
    roof_type: str | None,
    context: str,
    target_time: datetime | None = None,
    window_hours: float = 0,
) -> tuple[Any, Any, Any, Any, Any, Any, Any, Any, Any, Any, Any]:
    """(temp_f, feels_like_f, condition, icon, precip_type, wind_speed_mph,
    wind_gust_mph, wind_direction, precipitation_pct, visibility_mi, alert)
    for a stadium location - None across the board for an enclosed roof, a
    stadium with no known coordinates, or any fetch failure (weather is
    enrichment, never worth blocking the caller's write over). `context`
    only labels the log line on failure (e.g. "game_id=123") so it's
    traceable back to what the fetch was for. `icon` is Pirate Weather's
    own standardized identifier (e.g. "partly-cloudy-day", "rain",
    "clear-night") - meant for a UI icon set, distinct from `condition`'s
    free-text summary.

    `target_time`/`window_hours` scope which alerts are relevant -
    `target_time` defaults to now (the live in-game case: only an alert
    active at this instant matters). Pregame calls pass the game's actual
    kickoff and a multi-hour window instead, since the forecast/current
    conditions themselves are also "as of kickoff" for pregame, not "as of
    whenever this call happened to run" - alerts outside that window are
    filtered out rather than surfaced as if they applied to the game."""
    if roof_type in ENCLOSED_ROOF_TYPES or latitude is None or longitude is None:
        return _NO_WEATHER

    try:
        forecast = get_forecast(latitude, longitude)
    except Exception:
        logger.exception("Weather fetch failed for %s", context)
        return _NO_WEATHER

    current = forecast.currently
    if current is None:
        return _NO_WEATHER

    window_start = target_time or datetime.now(UTC)
    window_end = window_start + timedelta(hours=window_hours)
    alert = (
        "; ".join(
            a.title for a in forecast.alerts if _alert_overlaps(a, window_start, window_end)
        )
        or None
    )

    return (
        round(current.temperature) if current.temperature is not None else None,
        round(current.apparent_temperature)
        if current.apparent_temperature is not None
        else None,
        current.summary,
        current.icon,
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


def sql_batch_call(
    statements: list[tuple[str, list[Any] | None]], client: D1Client | None = None
) -> None:
    """Run a batch of (sql, params) statements"""
    client = client or D1Client(**get_d1_config())
    try:
        client.batch(statements)
    except D1Error:
        logger.exception("Loading data failed")
        sys.exit(1)


def id_map(client: D1Client, table: str, column: str, pk_column: str) -> dict[Any, int]:
    """external value : internal id for each row in the table"""
    result = client.query(
        f"SELECT {pk_column}, {column} FROM {table} WHERE {column} IS NOT NULL"
    )
    return {row[column]: row[pk_column] for row in result.results}


_UPSERT_MAPPING_GAP_SQL = """
INSERT INTO mapping_gaps (
    source, entity_type, raw_value, context, first_seen_at, last_seen_at
)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(source, entity_type, raw_value) DO UPDATE SET
    last_seen_at = excluded.last_seen_at,
    occurrences = occurrences + 1
"""


def mapping_gap_statement(
    source: str, entity_type: str, raw_value: Any, context: str | None = None
) -> tuple[str, list[Any] | None]:
    """(sql, params) for one mapping_gaps upsert - append to whatever
    statements list a loader is already building right next to its
    logger.warning() on a lookup miss"""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        _UPSERT_MAPPING_GAP_SQL,
        [source, entity_type, str(raw_value), context, now, now],
    )
