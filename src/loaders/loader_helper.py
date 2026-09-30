"""helper for src/loaders/"""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from api.weather_api import get_forecast
from api.weather_api_models import Alert, DailyDataPoint, DataPoint, Forecast
from db.clients import get_d1
from db.d1_client import D1Client, D1Error, Statement
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

# Stadiums with these roof types are always treated as enclosed - no way to
# detect actual roof state (e.g. a retractable roof open on a nice day), so
# weather is deliberately skipped for both rather than guessed at.
ENCLOSED_ROOF_TYPES = ("Dome", "Retractable")


class Weather(NamedTuple):
    """conditions at one moment - a game_snapshots row's weather columns or
    a games row's forecast_* kickoff columns, in column order"""

    temp_f: int | None = None
    feels_like_f: int | None = None
    condition: str | None = None
    icon: str | None = None
    precip_type: str | None = None
    wind_speed_mph: int | None = None
    wind_gust_mph: int | None = None
    wind_direction: str | None = None
    precipitation_pct: int | None = None
    visibility_mi: float | None = None
    alerts_json: str | None = None


class WeatherWindow(NamedTuple):
    """a pregame forecast across the game window - games.forecast_window_*
    plus forecast_hours_json, in column order"""

    precip_pct_max: int | None = None
    precip_type: str | None = None
    wind_gust_mph_max: int | None = None
    temp_f_low: int | None = None
    temp_f_high: int | None = None
    snow_accumulation_in: float | None = None
    hours_json: str | None = None


_NO_WEATHER = Weather()
_NO_WINDOW = WeatherWindow()

_COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]  # fmt: skip


def _bearing_to_compass(bearing: float) -> str:
    """Convert a wind bearing in degrees to a 16-point compass direction."""
    return _COMPASS_POINTS[round(bearing / 22.5) % 16]


# NWS alert types that are about the coastline/water, not conditions on the
# field - matched as a title prefix so every level (Watch/Warning/Advisory/
# Statement) of one type goes together. A denylist, not an allowlist: an
# alert type nobody's seen yet still gets shown, since surfacing an
# irrelevant alert is better than hiding a relevant one. Seen live on
# prod before this filter existed: Rip Current Statement, Beach Hazards
# Statement, Coastal Flood Warning/Advisory.
IRRELEVANT_ALERT_PREFIXES = (
    "Rip Current",
    "Beach Hazards",
    "Coastal Flood",
    "Lakeshore Flood",
    "High Surf",
    "Small Craft",
    "Gale",
    "Low Water",
    "Brisk Wind",
)


def is_game_relevant_alert(title: str) -> bool:
    """False for a coastal/marine alert type - see IRRELEVANT_ALERT_PREFIXES"""
    return not title.startswith(IRRELEVANT_ALERT_PREFIXES)


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


def _datapoint_to_weather(point: DataPoint, alerts: str | None) -> Weather:
    """one Pirate Weather DataPoint (`currently` or an `hourly.data[]`
    entry)"""
    return Weather(
        temp_f=round(point.temperature) if point.temperature is not None else None,
        feels_like_f=round(point.apparent_temperature)
        if point.apparent_temperature is not None
        else None,
        condition=point.summary,
        icon=point.icon,
        precip_type=point.precip_type,
        wind_speed_mph=round(point.wind_speed)
        if point.wind_speed is not None
        else None,
        wind_gust_mph=round(point.wind_gust) if point.wind_gust is not None else None,
        wind_direction=_bearing_to_compass(point.wind_bearing)
        if point.wind_bearing is not None
        else None,
        precipitation_pct=round(point.precip_probability * 100)
        if point.precip_probability is not None
        else None,
        visibility_mi=point.visibility,
        alerts_json=alerts,
    )


def _iso(epoch: int | None) -> str | None:
    return utc_iso(datetime.fromtimestamp(epoch, UTC)) if epoch is not None else None


def _overlapping_alerts(
    forecast: Forecast, window_start: datetime, window_hours: float
) -> str:
    """JSON list of the game-relevant alerts overlapping the window
    ('[]' if none) - {title, severity (NWS scale: Extreme/Severe/Moderate/
    Minor), starts, expires, uri}, for games.forecast_alerts_json/
    game_snapshots.weather_alerts_json"""
    window_end = window_start + timedelta(hours=window_hours)
    return json.dumps(
        [
            {
                "title": a.title,
                "severity": a.severity,
                "starts": _iso(a.time),
                "expires": _iso(a.expires),
                "uri": a.uri,
            }
            for a in forecast.alerts
            if is_game_relevant_alert(a.title)
            and _alert_overlaps(a, window_start, window_end)
        ]
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
) -> Weather:
    """Weather at a stadium location *right now* (Pirate Weather's `currently`) - the
    live in-game case. None across the board for an enclosed roof, unknown
    coordinates, or a failed fetch (see _fetch_outdoor_forecast()). `icon`
    is Pirate Weather's own standardized identifier (e.g.
    "partly-cloudy-day", "rain", "clear-night") - meant for a UI icon set,
    distinct from `condition`'s free-text summary. `alerts_json` is a JSON list
    (see _overlapping_alerts()) of game-relevant alerts active at this
    instant.

    Pregame wants the forecast *for kickoff*, not current conditions - see
    capture_pregame_forecast()."""
    forecast = _fetch_outdoor_forecast(latitude, longitude, roof_type, context)
    if forecast is None or forecast.currently is None:
        return _NO_WEATHER
    alerts = _overlapping_alerts(forecast, datetime.now(UTC), 0)
    return _datapoint_to_weather(forecast.currently, alerts)


def _hour_entry(point: DataPoint) -> dict[str, Any]:
    """one hourly entry for forecast_hours_json - the same per-point
    conversion as the kickoff forecast, minus visibility/alerts"""
    weather = _datapoint_to_weather(point, None)
    return {
        "time": _iso(point.time),
        "temp_f": weather.temp_f,
        "feels_like_f": weather.feels_like_f,
        "condition": weather.condition,
        "icon": weather.icon,
        "precip_type": weather.precip_type,
        "precipitation_pct": weather.precipitation_pct,
        "wind_speed_mph": weather.wind_speed_mph,
        "wind_gust_mph": weather.wind_gust_mph,
        "wind_direction": weather.wind_direction,
    }


def _window_summary(points: list[DataPoint]) -> WeatherWindow:
    """WeatherWindow across a run of hourly entries. precip_type is whatever's forecast at the wettest hour, not
    just the first non-null one - that's the hour that actually matters.
    hours_json is every entry itself (chronological, JSON text), so a UI
    can see which way it's trending - the aggregates alone can't say
    whether rain is rolling in or clearing out."""
    if not points:
        return _NO_WINDOW

    with_precip = [p for p in points if p.precip_probability is not None]
    wettest = (
        max(with_precip, key=lambda p: p.precip_probability or 0)
        if with_precip
        else None
    )
    gusts = [p.wind_gust for p in points if p.wind_gust is not None]
    temps = [p.temperature for p in points if p.temperature is not None]
    snow = [p.snow_accumulation for p in points if p.snow_accumulation is not None]

    return WeatherWindow(
        precip_pct_max=round((wettest.precip_probability or 0) * 100)
        if wettest
        else None,
        precip_type=wettest.precip_type if wettest else None,
        wind_gust_mph_max=round(max(gusts)) if gusts else None,
        temp_f_low=round(min(temps)) if temps else None,
        temp_f_high=round(max(temps)) if temps else None,
        snow_accumulation_in=round(sum(snow), 1) if snow else None,
        hours_json=json.dumps(
            [_hour_entry(p) for p in sorted(points, key=lambda p: p.time)]
        ),
    )


def _daily_to_forecast(
    day: DailyDataPoint, alerts: str | None
) -> tuple[Weather, WeatherWindow]:
    """a whole-day entry mapped onto the same (kickoff, window) shapes as the
    hourly path. There's no single temperature for a day, so kickoff
    temp_f/feels_like_f stay None and the day's min/max goes into the
    window's temp_f_low/high instead - a day-long range, not a game-window
    one, which is why forecast_source gets recorded alongside it. No
    hourly breakdown exists, so hours_json is an empty list."""
    precipitation_pct = (
        round(day.precip_probability * 100)
        if day.precip_probability is not None
        else None
    )
    wind_gust_mph = round(day.wind_gust) if day.wind_gust is not None else None
    kickoff = Weather(
        condition=day.summary,
        icon=day.icon,
        precip_type=day.precip_type,
        wind_speed_mph=round(day.wind_speed) if day.wind_speed is not None else None,
        wind_gust_mph=wind_gust_mph,
        wind_direction=_bearing_to_compass(day.wind_bearing)
        if day.wind_bearing is not None
        else None,
        precipitation_pct=precipitation_pct,
        visibility_mi=day.visibility,
        alerts_json=alerts,
    )
    window = WeatherWindow(
        precip_pct_max=precipitation_pct,
        precip_type=day.precip_type,
        wind_gust_mph_max=wind_gust_mph,
        temp_f_low=round(day.temperature_min)
        if day.temperature_min is not None
        else None,
        temp_f_high=round(day.temperature_max)
        if day.temperature_max is not None
        else None,
        snow_accumulation_in=round(day.snow_accumulation, 1)
        if day.snow_accumulation is not None
        else None,
        hours_json="[]",
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
) -> tuple[str | None, Weather, WeatherWindow]:
    """(source, kickoff, window) forecast for a game that hasn't started yet.

    source "hourly" (the normal case):
    - kickoff: a Weather like capture_weather()'s, but from the `hourly` entry
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
    any part of the game to be worth showing - and to game-relevant types
    only (is_game_relevant_alert()).

    (None, all-None, all-None) if kickoff is past both horizons (never falls
    back to `currently`), or for the same reasons as capture_weather()."""
    forecast = _fetch_outdoor_forecast(latitude, longitude, roof_type, context)
    if forecast is None:
        return None, _NO_WEATHER, _NO_WINDOW

    kickoff_ts = kickoff.timestamp()
    alerts = _overlapping_alerts(forecast, kickoff, alert_window_hours)

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
            _datapoint_to_weather(kickoff_point, alerts),
            _window_summary(window_points),
        )

    # daily entries start at local midnight, so the next entry's time (not
    # a fixed +24h, which DST would break) is where this day ends
    daily = forecast.daily.data if forecast.daily else []
    day_ends = [d.time for d in daily[1:]] + [daily[-1].time + 86400] if daily else []
    kickoff_day = next(
        (d for d, end in zip(daily, day_ends, strict=True) if d.time <= kickoff_ts < end),
        None,
    )
    if kickoff_day is not None:
        logger.info("Kickoff past the hourly horizon for %s, using daily", context)
        return "daily", *_daily_to_forecast(kickoff_day, alerts)

    logger.info("Kickoff past the daily forecast horizon for %s", context)
    return None, _NO_WEATHER, _NO_WINDOW


def sql_batch_call(
    statements: list[Statement], client: D1Client | None = None
) -> None:
    """Run a batch of (sql, params) statements. A D1Error is re-raised, not
    turned into sys.exit() - SystemExit isn't an Exception, so exiting here
    used to slip past orchestration.py's soft() and end the whole tick. Not
    logged here either: soft() logs it once."""
    client = client or get_d1()
    try:
        client.batch(statements)
    except D1Error as exc:
        exc.add_note("Loading data failed")
        raise


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
) -> Statement:
    """(sql, params) for one mapping_gaps upsert - append to whatever
    statements list a loader is already building right next to its
    logger.warning() on a lookup miss"""
    now = utc_iso()
    return (
        _UPSERT_MAPPING_GAP_SQL,
        [source, entity_type, str(raw_value), context, now, now],
    )
