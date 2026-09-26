"""Client for ESPN's undocumented public scoreboard endpoint - no API key,
no auth required. See api/espn_models.py's module docstring for the
"unofficial API" caveat.
"""

import logging
from typing import Any

import requests
import stamina

from api.api_helper import (
    RETRYABLE_STATUS,
    TIMEOUT_LIMIT,
    ApiDataError,
    ApiRateLimitError,
    ApiServerError,
    fetch_and_validate_one,
)
from api.espn_models import Scoreboard
from config.config import SEASON, configure_logging

SOURCE = "espn"

API_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

# regular season - same scope as the pool itself
REGULAR_SEASON_TYPE = 2

# Confirmed live 2026-09-09 by diffing the full 32-team abbreviation sets:
# ESPN uses "LAR"/"WSH" where teams.abbreviation (Sports IO's convention,
# which this project follows) uses "LA"/"WAS" - same pattern as
# cbs_client.ABBREV_CORRECTIONS, just for a different source.
ABBREV_CORRECTIONS = {
    "LAR": "LA",
    "WSH": "WAS",
}

logger: logging.Logger = logging.getLogger(__name__)


class EspnClient:
    def get_scoreboard(self, week: int | None = None) -> dict[str, Any]:
        """every NFL game for the current week (or a specific regular-season
        week of config.SEASON), live status/situation included"""
        params = None
        if week is not None:
            params = {"seasontype": REGULAR_SEASON_TYPE, "week": week, "dates": SEASON}
        return self._fetch(API_URL, params).json()

    @stamina.retry(
        on=(ApiRateLimitError, ApiServerError, requests.exceptions.RequestException)
    )
    def _fetch(
        self, url: str, params: dict[str, Any] | None = None
    ) -> requests.Response:
        response = requests.get(url, params=params, timeout=TIMEOUT_LIMIT)

        if response.status_code == 429:
            raise ApiRateLimitError(SOURCE, f"Rate limited fetching {url}")
        if response.status_code in RETRYABLE_STATUS:
            raise ApiServerError(
                SOURCE, f"ESPN returned {response.status_code} for {url}"
            )
        if response.status_code != 200:
            raise ApiDataError(
                SOURCE,
                f"ESPN returned {response.status_code} for {url}: {response.text}",
            )
        return response


def get_scoreboard(week: int | None = None) -> Scoreboard:
    """current week's NFL scoreboard, or a specific regular-season week's"""
    return fetch_and_validate_one(
        "ESPN scoreboard", lambda: EspnClient().get_scoreboard(week), Scoreboard
    )


if __name__ == "__main__":
    configure_logging()
    get_scoreboard()
