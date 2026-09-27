"""helper for src/loaders/"""

import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from api.weather_api import get_forecast
from api.weather_api_models import Alert, DailyDataPoint, DataPoint, Forecast
from config.config import get_d1_config
from db.d1_client import D1Client, D1Error

logger = logging.getLogger(__name__)

# Stadiums with these roof types are always treated as enclosed - no way to
# detect actual roof state (e.g. a retractable roof open on a nice day), so
# weather is deliberately skipped for both rather than guessed at.
ENCLOSED_ROOF_TYPES = ("Dome", "Retractable")

_NO_WEATHER = (None, None, None, None, None, None, None, None, None, None, None)
_NO_WINDOW = (None, None, None, None, None, None)

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


def _datapoint_to_weather(
    point: DataPoint, alert: str | None
) -> tuple[Any, Any, Any, Any, Any, Any, Any, Any, Any, Any, Any]:
    """one Pirate Weather DataPoint (`currently` or an `hourly.data[]`
    entry) as capture_weather()'s tuple shape"""
    return (
        round(point.temperature) if point.temperature is not None else None,
        round(point.apparent_temperature)
        if point.apparent_temperature is not None
        else None,
        point.summary,
        point.icon,
        point.precip_type,
        round(point.wind_speed) if point.wind_speed is not None else None,
        round(point.wind_gust) if point.wind_gust is not None else None,
        _bearing_to_compass(point.wind_bearing)
        if point.wind_bearing is not None
        else None,
        round(point.precip_probability * 100)
        if point.precip_probability is not None
        else None,
        point.visibility,
        alert,
    )


def _overlapping_alerts(
    forecast: Forecast, window_start: datetime, window_hours: float
) -> str | None:
    window_end = window_start + timedelta(hours=window_hours)
    return (
        "; ".join(
            a.title for a in forecast.alerts if _alert_overlaps(a, window_start, window_end)
        )
        or None
    )


def _fetch_outdoor_forecast(
    latitude: float | None,
    longitude: float | None,
    roof_type: str | None,
    context: str,
) -> Forecast | None:
    """None for an enclosed roof, a stadium with no known coordinates, or
    any fetch failure (weather is enrichment, never worth blocking the
    caller's write over). `context` only labels the log line on failure
    (e.g. "game_id=123") so it's traceable back to what the fetch was for."""
    if roof_type in ENCLOSED_ROOF_TYPES or latitude is None or longitude is None:
        return None
    try:
        return get_forecast(latitude, longitude)
    except Exception:
        logger.exception("Weather fetch failed for %s", context)
        return None


def capture_weather(
    latitude: float | None,
    longitude: float | None,
    roof_type: str | None,
    context: str,
) -> tuple[Any, Any, Any, Any, Any, Any, Any, Any, Any, Any, Any]:
    """(temp_f, feels_like_f, condition, icon, precip_type, wind_speed_mph,
    wind_gust_mph, wind_direction, precipitation_pct, visibility_mi, alert)
    for a stadium location *right now* (Pirate Weather's `currently`) - the
    live in-game case. None across the board for an enclosed roof, unknown
    coordinates, or a failed fetch (see _fetch_outdoor_forecast()). `icon`
    is Pirate Weather's own standardized identifier (e.g.
    "partly-cloudy-day", "rain", "clear-night") - meant for a UI icon set,
    distinct from `condition`'s free-text summary. Only an alert active at
    this instant is attached.

    Pregame wants the forecast *for kickoff*, not current conditions - see
    capture_pregame_forecast()."""
    forecast = _fetch_outdoor_forecast(latitude, longitude, roof_type, context)
    if forecast is None or forecast.currently is None:
        return _NO_WEATHER
    alert = _overlapping_alerts(forecast, datetime.now(UTC), 0)
    return _datapoint_to_weather(forecast.currently, alert)


def _window_summary(
    points: list[DataPoint],
) -> tuple[Any, Any, Any, Any, Any, Any]:
    """(precip_pct_max, precip_type, wind_gust_mph_max, temp_f_low,
    temp_f_high, snow_accumulation_in) across a run of hourly entries.
    precip_type is whatever's forecast at the wettest hour, not just the
    first non-null one - that's the hour that actually matters."""
    if not points:
        return _NO_WINDOW

    with_precip = [p for p in points if p.precip_probability is not None]
    wettest = (
        max(with_precip, key=lambda p: p.precip_probability or 0) if with_precip else None
    )
    gusts = [p.wind_gust for p in points if p.wind_gust is not None]
    temps = [p.temperature for p in points if p.temperature is not None]
    snow = [p.snow_accumulation for p in points if p.snow_accumulation is not None]

    return (
        round((wettest.precip_probability or 0) * 100) if wettest else None,
        wettest.precip_type if wettest else None,
        round(max(gusts)) if gusts else None,
        round(min(temps)) if temps else None,
        round(max(temps)) if temps else None,
        round(sum(snow), 1) if snow else None,
    )


def _daily_to_forecast(
    day: DailyDataPoint, alert: str | None
) -> tuple[
    tuple[Any, Any, Any, Any, Any, Any, Any, Any, Any, Any, Any],
    tuple[Any, Any, Any, Any, Any, Any],
]:
    """a whole-day entry mapped onto the same (kickoff, window) shapes as the
    hourly path. There's no single temperature for a day, so kickoff
    temp_f/feels_like_f stay None and the day's min/max goes into the
    window's temp_f_low/high instead - a day-long range, not a game-window
    one, which is why forecast_source gets recorded alongside it."""
    kickoff = (
        None,
        None,
        day.summary,
        day.icon,
        day.precip_type,
        round(day.wind_speed) if day.wind_speed is not None else None,
        round(day.wind_gust) if day.wind_gust is not None else None,
        _bearing_to_compass(day.wind_bearing) if day.wind_bearing is not None else None,
        round(day.precip_probability * 100)
        if day.precip_probability is not None
        else None,
        day.visibility,
        alert,
    )
    window = (
        round(day.precip_probability * 100)
        if day.precip_probability is not None
        else None,
        day.precip_type,
        round(day.wind_gust) if day.wind_gust is not None else None,
        round(day.temperature_min) if day.temperature_min is not None else None,
        round(day.temperature_max) if day.temperature_max is not None else None,
        round(day.snow_accumulation, 1) if day.snow_accumulation is not None else None,
    )
    return kickoff, window


def capture_pregame_forecast(
    latitude: float | None,
    longitude: float | None,
    roof_type: str | None,
    context: str,
    kickoff: datetime,
    window_hours: float,
    alert_window_hours: float,
) -> tuple[
    str | None,
    tuple[Any, Any, Any, Any, Any, Any, Any, Any, Any, Any, Any],
    tuple[Any, Any, Any, Any, Any, Any],
]:
    """(source, kickoff, window) forecast for a game that hasn't started yet.

    source "hourly" (the normal case):
    - kickoff: same shape as capture_weather(), but from the `hourly` entry
      whose hour contains kickoff - not `currently`, which would just be
      conditions whenever this capture happened to run (e.g. Tuesday's
      weather for a Sunday game).
    - window: see _window_summary(), across the hourly entries overlapping
      [kickoff, kickoff + window_hours) - catches rain/wind forecast to roll
      in after kickoff but while the game's still being played.

    source "daily": kickoff is past the hourly horizon (168h, confirmed live
    2026-09-27) but within daily's (8 days) - the kickoff day's entry, see
    _daily_to_forecast(). Coarser, but better than nothing; replaced by
    hourly on the first capture once kickoff comes within range, since
    every capture overwrites in place.

    Alerts are filtered against the separate, usually longer
    [kickoff, kickoff + alert_window_hours] - an alert only needs to touch
    any part of the game to be worth showing.

    (None, all-None, all-None) if kickoff is past both horizons (never falls
    back to `currently`), or for the same reasons as capture_weather()."""
    forecast = _fetch_outdoor_forecast(latitude, longitude, roof_type, context)
    if forecast is None:
        return None, _NO_WEATHER, _NO_WINDOW

    kickoff_ts = kickoff.timestamp()
    alert = _overlapping_alerts(forecast, kickoff, alert_window_hours)

    hourly = forecast.hourly.data if forecast.hourly else []
    kickoff_point = next(
        (p for p in hourly if p.time <= kickoff_ts < p.time + 3600), None
    )
    if kickoff_point is not None:
        window_end_ts = kickoff_ts + window_hours * 3600
        window_points = [
            p for p in hourly if p.time < window_end_ts and p.time + 3600 > kickoff_ts
        ]
        return (
            "hourly",
            _datapoint_to_weather(kickoff_point, alert),
            _window_summary(window_points),
        )

    # daily entries start at local midnight, so the next entry's time (not
    # a fixed +24h, which DST would break) is where this day ends
    daily = forecast.daily.data if forecast.daily else []
    day_ends = [d.time for d in daily[1:]] + [daily[-1].time + 86400] if daily else []
    kickoff_day = next(
        (d for d, end in zip(daily, day_ends) if d.time <= kickoff_ts < end), None
    )
    if kickoff_day is not None:
        logger.info("Kickoff past the hourly horizon for %s, using daily", context)
        return "daily", *_daily_to_forecast(kickoff_day, alert)

    logger.info("Kickoff past the daily forecast horizon for %s", context)
    return None, _NO_WEATHER, _NO_WINDOW


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
