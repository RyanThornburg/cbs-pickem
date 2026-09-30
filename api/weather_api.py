"""Client for reading weather data from Pirate Weather
(https://pirate-weather.apiable.io/full-api-reference), a Dark Sky
API-compatible replacement.
"""

import logging
from functools import cache
from typing import Any

import requests
import stamina

from api.api_helper import (
    TIMEOUT_LIMIT,
    ApiRateLimitError,
    ApiServerError,
    check_response,
    fetch_and_validate_one,
)
from api.weather_api_models import Forecast
from config.config import configure_logging, get_weather_api

SOURCE = "pirate_weather"

API_URL = "https://api.pirateweather.net/forecast"

# We only need current conditions (live game), hourly for kickoff, and daily
# as the pregame fallback when kickoff is past the hourly horizon (hourly
# reaches 168h, daily 8 days - confirmed live 2026-09-27)
EXCLUDE = "minutely,flags"
# extend=hourly: hourly defaults to only the next 48h, not the full 7 days -
# needed so a forecast checked early in the week still covers Sunday kickoff.
# version=2: unlocks snow/ice/liquid accumulation fields on hourly entries,
# more directly game-relevant than just precipType/precipIntensity.
EXTRA_PARAMS = {"exclude": EXCLUDE, "extend": "hourly", "units": "us", "version": "2"}

LOW_CREDIT_THRESHOLD = 500


logger: logging.Logger = logging.getLogger(__name__)


class WeatherApiClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def get_forecast(self, lat: float, lng: float) -> dict[str, Any]:
        """current conditions + up to 7 days hourly forecast for a location"""
        url = f"{API_URL}/{self.api_key}/{lat},{lng}"
        return self._fetch(url).json()

    def _record_quota(self, response: requests.Response) -> None:
        remaining = response.headers.get("X-Ratelimit-Remaining-Month")
        if remaining is not None and int(remaining) <= LOW_CREDIT_THRESHOLD:
            logger.warning(
                "Pirate Weather usage credits running low: %s remaining this month",
                remaining,
            )

    @stamina.retry(
        on=(ApiRateLimitError, ApiServerError, requests.exceptions.RequestException)
    )
    def _fetch(self, url: str) -> requests.Response:
        """fetch a forecast, retry transient/rate-limit/network errors with backoff"""
        response = requests.get(url, params=EXTRA_PARAMS, timeout=TIMEOUT_LIMIT)
        self._record_quota(response)
        # API_URL, not `url` - the key is in the path
        check_response(SOURCE, "Pirate Weather", API_URL, response)
        return response


@cache
def _new_client() -> WeatherApiClient:
    """one client per process - reads the key once"""
    return WeatherApiClient(get_weather_api())


def get_forecast(lat: float, lng: float) -> Forecast:
    """current conditions + hourly forecast for a stadium location"""
    return fetch_and_validate_one(
        "weather forecast", lambda: _new_client().get_forecast(lat, lng), Forecast
    )


if __name__ == "__main__":
    configure_logging()
    get_forecast(39.0489, -94.4839)
