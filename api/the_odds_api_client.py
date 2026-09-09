"""Client for reading odds data from The Odds API"""

import logging

import requests
import stamina

from api.api_helper import (
    RETRYABLE_STATUS,
    TIMEOUT_LIMIT,
    ApiDataError,
    ApiRateLimitError,
    ApiServerError,
    fetch_and_validate,
)
from api.the_odds_api_models import Event
from config.config import configure_logging, get_the_odds_api

SOURCE = "the_odds_api"

API_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"
REGIONS = "us"
MARKETS = "spreads,totals,h2h"

# odds api allows 30 calls/second and only returns month usage
# setting a threshold to warn when we approach it
LOW_CREDIT_THRESHOLD = 50


logger: logging.Logger = logging.getLogger(__name__)


class TheOddsApiClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def get_odds(self):
        """pre-game odds (spreads/totals) for every upcoming/live NFL game"""
        params = {
            "api_key": self.api_key,
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": "american",
        }
        return self._fetch(params).json()

    def _record_quota(self, response: requests.Response) -> None:
        remaining = response.headers.get("x-requests-remaining")
        if remaining is not None and int(remaining) <= LOW_CREDIT_THRESHOLD:
            logger.warning(
                "The Odds API usage credits running low: %s remaining", remaining
            )

    @stamina.retry(
        on=(ApiRateLimitError, ApiServerError, requests.exceptions.RequestException)
    )
    def _fetch(self, params: dict[str, str]) -> requests.Response:
        """fetch odds, retry transient/rate-limit/network errors with backoff"""
        response = requests.get(API_URL, params=params, timeout=TIMEOUT_LIMIT)
        self._record_quota(response)

        if response.status_code == 429:
            raise ApiRateLimitError(SOURCE, f"Rate limited fetching {API_URL}")
        if response.status_code in RETRYABLE_STATUS:
            raise ApiServerError(
                SOURCE, f"The Odds API returned {response.status_code} for {API_URL}"
            )
        if response.status_code != 200:
            raise ApiDataError(
                SOURCE,
                f"The Odds API returned {response.status_code} for {API_URL}: {response.text}",
            )
        return response


def _new_client() -> TheOddsApiClient:
    return TheOddsApiClient(get_the_odds_api())


def get_odds() -> list[Event]:
    """init the odds api client and fetch/validate odds"""
    return fetch_and_validate("odds", _new_client().get_odds, Event)


if __name__ == "__main__":
    configure_logging()
    get_odds()
