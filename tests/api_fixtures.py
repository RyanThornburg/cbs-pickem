"""Saved raw API responses (tests/fixtures/api/, captured by
tests/fixtures/capture_api.py) and the patches that make each API client
return them instead of calling out - the clients' real parsing and
pydantic validation still run on every test."""

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from api import (
    cbs_client,
    espn_client,
    sports_io_client,
    the_odds_api_client,
    weather_api,
)
from api.sports_io_client import Endpoint

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "api"
CBS_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "cbs"

# one Sports IO endpoint -> its saved response
SPORTS_IO_FIXTURES = {
    Endpoint.GAMES: "sports_io_games.json",
    Endpoint.TEAMS: "sports_io_teams.json",
    Endpoint.STANDINGS: "sports_io_standings.json",
    Endpoint.TEAM_STATISTICS: "sports_io_team_statistics.json",
    Endpoint.GAME_EVENTS: "sports_io_game_events.json",
    Endpoint.PLAYER_STATISTICS: "sports_io_player_statistics.json",
}


@cache
def _read(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def fixture(name: str) -> Any:
    """a fresh copy each call, so a test can edit it freely"""
    return copy.deepcopy(_read(name))


def capture_info() -> dict[str, Any]:
    """which week/game the fixtures were captured around"""
    return _read("capture.json")


class FakeApis:
    """Serves saved responses to every API client. Override any response
    with `responses[...]`: Sports IO by Endpoint (a callable taking the
    request's params, or a list), ESPN by "scoreboard"/"summary", plus
    "odds" and "weather". `calls` records each Sports IO request."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.responses: dict[Any, Any] = {
            endpoint: fixture(name) for endpoint, name in SPORTS_IO_FIXTURES.items()
        }
        self.responses |= {
            "scoreboard": fixture("espn_scoreboard.json"),
            "summary": fixture("espn_summary.json"),
            "odds": fixture("the_odds_api_odds.json"),
            "weather": fixture("pirate_weather_forecast.json"),
        }
        self.calls: list[tuple[Endpoint, dict[str, Any]]] = []
        self.weather_calls = 0
        self.summary_calls: list[str] = []
        apis = self

        class SportsIO(sports_io_client.SportsIOClient):
            def _request(self, endpoint: Endpoint, **params: Any) -> list[Any]:
                apis.calls.append((endpoint, params))
                response = apis.responses[endpoint]
                return (
                    response(params) if callable(response) else copy.deepcopy(response)
                )

        class Espn(espn_client.EspnClient):
            def get_scoreboard(self, week: int | None = None) -> dict[str, Any]:
                return apis._respond("scoreboard", week)

            def get_summary(self, event_id: str) -> dict[str, Any]:
                apis.summary_calls.append(event_id)
                return apis._respond("summary", event_id)

        class Odds(the_odds_api_client.TheOddsApiClient):
            def get_odds(self) -> list[dict[str, Any]]:
                return apis._respond("odds", None)

        class Weather(weather_api.WeatherApiClient):
            def get_forecast(self, lat: float, lng: float) -> dict[str, Any]:
                apis.weather_calls += 1
                return apis._respond("weather", (lat, lng))

        monkeypatch.setattr(sports_io_client, "_new_client", lambda: SportsIO(""))
        monkeypatch.setattr(espn_client, "EspnClient", Espn)
        monkeypatch.setattr(the_odds_api_client, "_new_client", lambda: Odds(""))
        monkeypatch.setattr(weather_api, "_new_client", lambda: Weather(""))

    def _respond(self, key: str, arg: Any) -> Any:
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return response(arg) if callable(response) else copy.deepcopy(response)


class FakeCBS:
    """cbs_client serving the saved (anonymized) CBS pages instead of
    logging in with Playwright - the week's pages by default, or any
    week's by pool_period_id"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, week: int) -> None:
        self.home = json.loads(
            (CBS_FIXTURE_DIR / f"cbs_pool_home_{week:02d}.json").read_text(
                encoding="utf-8"
            )
        )
        self.weekly = json.loads(
            (CBS_FIXTURE_DIR / f"cbs_week_{week:02d}.json").read_text(encoding="utf-8")
        )
        entries = self.weekly["standings"]["weekly"]["rankedEntries"]
        self.members = [dict(e["entry"]["member"], email=None) for e in entries]
        fake = self

        class Client:
            pool_period_id: str | None = None

            def fetch_pool_home_data(self) -> dict[str, Any]:
                return json.loads(json.dumps(fake.home))

            def fetch_weekly_data(self) -> dict[str, Any]:
                return json.loads(json.dumps(fake.weekly))

            def fetch_user_data(self) -> dict[str, Any]:
                return {"members": fake.members}

        monkeypatch.setattr(cbs_client, "_new_client", Client)
        monkeypatch.setattr(cbs_client, "write_data", lambda data, path: None)
