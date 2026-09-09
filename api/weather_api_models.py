"""
Used Claude to model the data

Pydantic models for Pirate Weather's forecast response
(https://pirate-weather.apiable.io/full-api-reference)
Only the fields relevant to game-impact weather are modeled;
`daily`/`minutely`/`flags` blocks are excluded at the request level
(see api/weather_api.py's EXCLUDE) rather than modeled and discarded.

`Alert`'s shape (title/severity/time/expires/description/uri/regions) is
confirmed against Pirate Weather's own OpenAPI spec - not yet seen in a live
non-empty response though, since no alert was active at build time (confirmed
live 2026-09-09 that `alerts` comes back as `[]` for a real location/time).
"""

from pydantic import BaseModel, ConfigDict, Field


class WeatherApiModel(BaseModel):
    """Base for every typed Pirate Weather entity - tolerate unknown fields
    (it's a large Dark Sky-compatible payload, most of which isn't modeled)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class DataPoint(WeatherApiModel):
    """Shared shape for `currently` and each `hourly.data[]` entry.
    snow_accumulation/ice_accumulation/liquid_accumulation only populate on
    hourly entries (not `currently`) and only with `version=2` requested."""

    time: int  # epoch seconds
    summary: str | None = None
    icon: str | None = None
    precip_intensity: float | None = Field(default=None, alias="precipIntensity")
    precip_probability: float | None = Field(default=None, alias="precipProbability")
    precip_type: str | None = Field(default=None, alias="precipType")
    snow_accumulation: float | None = Field(default=None, alias="snowAccumulation")
    ice_accumulation: float | None = Field(default=None, alias="iceAccumulation")
    liquid_accumulation: float | None = Field(default=None, alias="liquidAccumulation")
    temperature: float | None = None
    apparent_temperature: float | None = Field(
        default=None, alias="apparentTemperature"
    )
    humidity: float | None = None
    wind_speed: float | None = Field(default=None, alias="windSpeed")
    wind_gust: float | None = Field(default=None, alias="windGust")
    wind_bearing: int | None = Field(default=None, alias="windBearing")
    cloud_cover: float | None = Field(default=None, alias="cloudCover")
    visibility: float | None = None


class HourlyBlock(WeatherApiModel):
    """hourly weather summary"""

    summary: str | None = None
    icon: str | None = None
    data: list[DataPoint] = Field(default_factory=list[DataPoint])


class Alert(WeatherApiModel):
    """alert object"""

    title: str
    severity: str  # e.g. 'Advisory', 'Watch', 'Warning'
    time: int
    expires: int | None = None
    description: str | None = None
    uri: str | None = None
    regions: list[str] = Field(default_factory=list[str])


class Forecast(WeatherApiModel):
    """forecast"""

    latitude: float
    longitude: float
    timezone: str
    currently: DataPoint | None = None
    hourly: HourlyBlock | None = None
    alerts: list[Alert] = Field(default_factory=list[Alert])
