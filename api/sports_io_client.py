"""Client for reading data from sports io api"""

import logging
import time
from enum import Enum
from typing import Any

import requests
import stamina
from pydantic import BaseModel

from api.api_helper import (
    RETRYABLE_STATUS,
    TIMEOUT_LIMIT,
    ApiDataError,
    ApiRateLimitError,
    ApiServerError,
    fetch_and_validate,
)
from api.sports_io_models import (
    Game,
    GameEvent,
    LeagueSeason,
    LeagueSeasons,
    Odds,
    Standing,
    Team,
    TeamStatistics,
)
from config.config import SEASON, configure_logging, get_sports_io_api

SOURCE = "sports_io"

LEAGUE_ID = 1  # NFL League ID
API_URL = "https://v1.american-football.api-sports.io"

# api-sports appends both a per-minute (`x-ratelimit-*`) and a daily
# (`x-ratelimit-requests-*`) quota to every response
# use these to proactively check and handle vs react to 429
LOW_MINUTE_QUOTA_THRESHOLD = 5
MINUTE_QUOTA_BACKOFF_SECONDS = 5.0
LOW_DAILY_QUOTA_THRESHOLD = 100


class Endpoint(Enum):
    """endpoint and the model to validate"""

    LEAGUES = ("/leagues", LeagueSeasons)
    TEAMS = ("/teams", Team)
    STANDINGS = ("/standings", Standing)
    GAMES = ("/games", Game)
    TEAM_STATISTICS = ("/games/statistics/teams", TeamStatistics)
    GAME_EVENTS = ("/games/events", GameEvent)
    ODDS = ("/odds", Odds)

    def __init__(self, path: str, model: type[BaseModel]):
        self.path = path
        self.model = model


_LEAGUE_SEASON_ENDPOINTS = {Endpoint.TEAMS, Endpoint.STANDINGS, Endpoint.GAMES}


logger: logging.Logger = logging.getLogger(__name__)


class SportsIOClient:
    def __init__(self, api_key: str):
        self.season: int = SEASON
        self.headers = {"x-apisports-key": api_key}
        self._minute_remaining: int | None = None

    def get_current_season(self):
        """current season (year, start/end dates) for the configured league"""
        return self._request(Endpoint.LEAGUES, id=LEAGUE_ID, current="true")

    def get_teams(self):
        """team profiles"""
        return self._request(Endpoint.TEAMS)

    def get_standings(self):
        """league standings"""
        # api-sports.io has a placeholder row, dropping it here
        return [
            item
            for item in self._request(Endpoint.STANDINGS)
            if item.get("team", {}).get("name") is not None
        ]

    def get_games(self):
        """schedule/live scores/results"""
        return self._request(Endpoint.GAMES)

    def get_live_games(self):
        """live games"""
        return self._request(Endpoint.GAMES, live="all")

    def get_games_by_date(self, date: str):
        """every game (any status) on a given date (YYYY-MM-DD)"""
        return self._request(Endpoint.GAMES, date=date)

    def get_team_statistics(self, game_id: int):
        """box scores for teams"""
        return self._request(Endpoint.TEAM_STATISTICS, id=game_id)

    def get_game_events(self, game_id: int):
        """scoring plays"""
        return self._request(Endpoint.GAME_EVENTS, id=game_id)

    def get_odds(self, game_id: int):
        """pre-game odds from api"""
        return self._request(Endpoint.ODDS, game=game_id)

    def _throttle_if_needed(self) -> None:
        """backoff and throttle as needed"""
        if (
            self._minute_remaining is not None
            and self._minute_remaining <= LOW_MINUTE_QUOTA_THRESHOLD
        ):
            logger.warning(
                "Sports IO per-minute quota nearly exhausted (%d remaining), pausing %.0fs",
                self._minute_remaining,
                MINUTE_QUOTA_BACKOFF_SECONDS,
            )
            time.sleep(MINUTE_QUOTA_BACKOFF_SECONDS)

    def _record_quota(self, response: requests.Response) -> None:
        """check and record our quotes/backoffs to prevent 429s"""
        minute_remaining = response.headers.get("x-ratelimit-remaining")
        if minute_remaining is not None:
            self._minute_remaining = int(minute_remaining)

        daily_remaining = response.headers.get("x-ratelimit-requests-remaining")
        if (
            daily_remaining is not None
            and int(daily_remaining) <= LOW_DAILY_QUOTA_THRESHOLD
        ):
            logger.warning(
                "Sports IO daily quota running low: %s requests remaining",
                daily_remaining,
            )

    @stamina.retry(
        on=(ApiRateLimitError, ApiServerError, requests.exceptions.RequestException)
    )
    def _fetch(self, url: str, params: dict[str, Any]) -> requests.Response:
        """fetch `url`, retry transient/rate-limit/network errors with backoff"""
        self._throttle_if_needed()

        response = requests.get(
            url, headers=self.headers, params=params, timeout=TIMEOUT_LIMIT
        )
        self._record_quota(response)

        if response.status_code == 429:
            raise ApiRateLimitError(SOURCE, f"Rate limited fetching {url}")
        if response.status_code in RETRYABLE_STATUS:
            raise ApiServerError(
                SOURCE, f"Sports IO returned {response.status_code} for {url}"
            )
        if response.status_code != 200:
            raise ApiDataError(
                SOURCE,
                f"Sports IO returned {response.status_code} for {url}: {response.text}",
            )
        return response

    def _fetch_page(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._fetch(url, params)
        try:
            data = response.json()
        except ValueError as e:
            raise ApiDataError(
                SOURCE, f"Sports IO returned non-JSON body for {url}"
            ) from e

        errors = data.get("errors")
        if errors:
            raise ApiDataError(SOURCE, f"Sports IO returned errors for {url}: {errors}")

        return data

    def _request(self, endpoint: Endpoint, **params: Any) -> list[Any]:
        """fetch endpoint and handle pagination"""
        if endpoint in _LEAGUE_SEASON_ENDPOINTS:
            params = {"league": LEAGUE_ID, "season": self.season, **params}
        url = f"{API_URL}{endpoint.path}"

        items: list[Any] = []
        page = 1
        while True:
            data = self._fetch_page(
                url, {**params, "page": page} if page > 1 else params
            )
            items.extend(data["response"])

            paging = data.get("paging")
            if not paging or paging["current"] >= paging["total"]:
                return items

            logger.info(
                "Sports IO %s is paginated (page %d/%d) - fetching next page",
                endpoint.path,
                paging["current"],
                paging["total"],
            )
            page += 1


def _new_client() -> SportsIOClient:
    return SportsIOClient(get_sports_io_api())


def get_current_season() -> LeagueSeason | None:
    """current NFL season's year and start/end dates, from api-sports.io's
    /leagues?current=true. None if the API has no season flagged current,
    or if its season doesn't match config.SEASON (a stale SEASON is unsafe
    to keep going on, not just worth a warning)."""
    leagues = fetch_and_validate(
        "current season", _new_client().get_current_season, Endpoint.LEAGUES.model
    )
    for league in leagues:
        for season in league.seasons:
            if season.current:
                if season.year != SEASON:
                    logger.warning(
                        "Sports IO's current season (%d) doesn't match "
                        "config.SEASON (%d)! Update config and run again",
                        season.year,
                        SEASON,
                    )
                    return None
                return season
    return None


def get_teams() -> list[Team]:
    """get basic team data from api - likely not needed often after season starts"""
    return fetch_and_validate("teams", _new_client().get_teams, Endpoint.TEAMS.model)


def get_standings() -> list[Standing]:
    """standings from api - can run either on game ends or x intervals on game days"""
    return fetch_and_validate(
        "standings", _new_client().get_standings, Endpoint.STANDINGS.model
    )


def get_live_games() -> list[Game]:
    """check for live games"""
    return fetch_and_validate(
        "live games", _new_client().get_live_games, Endpoint.GAMES.model
    )


def get_games_by_date(date: str) -> list[Game]:
    """every game (any status, including FINAL) on a given date (YYYY-MM-DD)"""
    return fetch_and_validate(
        f"games on {date}",
        lambda: _new_client().get_games_by_date(date),
        Endpoint.GAMES.model,
    )


def get_team_statistics(game_id: int) -> list[TeamStatistics]:
    """run x interval when games are live"""
    return fetch_and_validate(
        "team statistics",
        lambda: _new_client().get_team_statistics(game_id),
        Endpoint.TEAM_STATISTICS.model,
    )


def get_games() -> list[Game]:
    """run x interval when games are live"""
    return fetch_and_validate("games", _new_client().get_games, Endpoint.GAMES.model)


def get_game_events(game_id: int) -> list[GameEvent]:
    """run x interval when games are live"""
    return fetch_and_validate(
        "game events",
        lambda: _new_client().get_game_events(game_id),
        Endpoint.GAME_EVENTS.model,
    )


def get_odds(game_id: int) -> list[Odds]:
    """refresh a few times a day (if using)"""
    return fetch_and_validate(
        "odds", lambda: _new_client().get_odds(game_id), Endpoint.ODDS.model
    )


if __name__ == "__main__":
    configure_logging()
    get_games()
