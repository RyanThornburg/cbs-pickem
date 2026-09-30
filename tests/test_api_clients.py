"""api/ - every saved response still parses through its client's models,
plus each client's HTTP handling (error statuses, Sports IO's error-on-200
envelope, pagination, quota headers) against fake responses."""

from collections.abc import Iterator
from typing import Any

import pytest
import requests
import stamina

from api import espn_client, sports_io_client, the_odds_api_client, weather_api
from api.api_helper import ApiDataError, ApiRateLimitError, ApiServerError
from api.sports_io_client import Endpoint, SportsIOClient
from api.weather_api import WeatherApiClient
from config.config import SEASON
from tests.api_fixtures import FakeApis, capture_info


@pytest.fixture(autouse=True)
def _no_retry_waits() -> Iterator[None]:
    # one attempt, no backoff sleeps, for the retried error paths
    stamina.set_testing(True, attempts=1)
    yield
    stamina.set_testing(False)


class TestSavedResponsesParse:
    def test_sports_io(self, apis: FakeApis) -> None:
        games = sports_io_client.get_games()
        assert {g.game.stage for g in games} >= {"Regular Season", "Pre Season"}
        assert len(sports_io_client.get_teams()) == 32
        assert len(sports_io_client.get_standings()) == 32
        game_id = capture_info()["sports_io_game_id"]
        stats = sports_io_client.get_team_statistics(game_id)
        assert len(stats) == 2
        events = sports_io_client.get_game_events(game_id)
        assert events and all(e.score.home + e.score.away > 0 for e in events)
        players = sports_io_client.get_player_statistics(game_id)
        assert {g.name for team in players for g in team.groups} >= {
            "Passing",
            "Rushing",
            "Receiving",
        }

    def test_espn(self, apis: FakeApis) -> None:
        scoreboard = espn_client.get_scoreboard(capture_info()["week"])
        assert len(scoreboard.events) == 16
        for event in scoreboard.events:
            assert {c.home_away for c in event.competitions[0].competitors} == {
                "home",
                "away",
            }
        summary = espn_client.get_summary(capture_info()["espn_event_id"])
        assert summary.win_probability
        assert summary.drives is not None and summary.drives.previous

    def test_odds(self, apis: FakeApis) -> None:
        events = the_odds_api_client.get_odds()
        markets = {m.key for e in events for b in e.bookmakers for m in b.markets}
        assert markets == {"spreads", "totals", "h2h"}

    def test_weather(self, apis: FakeApis) -> None:
        forecast = weather_api.get_forecast(44.5, -88.0)
        assert forecast.currently is not None
        assert forecast.hourly is not None and len(forecast.hourly.data) == 168
        assert forecast.daily is not None and len(forecast.daily.data) == 8


class TestCurrentSeason:
    def _league(self, year: int, current: bool = True) -> list[dict[str, Any]]:
        return [
            {
                "league": {"id": 1, "name": "NFL"},
                "seasons": [
                    {
                        "year": year - 1,
                        "start": "2025-08-01",
                        "end": "2026-02-10",
                        "current": False,
                    },
                    {
                        "year": year,
                        "start": "2026-07-30",
                        "end": "2027-02-14",
                        "current": current,
                    },
                ],
            }
        ]

    def test_current_season(self, apis: FakeApis) -> None:
        apis.responses[Endpoint.LEAGUES] = self._league(SEASON)
        season = sports_io_client.get_current_season()
        assert season is not None and season.year == SEASON

    def test_stale_config_season_fails_closed(self, apis: FakeApis) -> None:
        apis.responses[Endpoint.LEAGUES] = self._league(SEASON + 1)
        assert sports_io_client.get_current_season() is None

    def test_no_current_season(self, apis: FakeApis) -> None:
        apis.responses[Endpoint.LEAGUES] = self._league(SEASON, current=False)
        assert sports_io_client.get_current_season() is None


def test_standings_drop_the_placeholder_row(apis: FakeApis) -> None:
    # api-sports mixes a nameless, zeroed row into /standings
    placeholder = {
        **apis.responses[Endpoint.STANDINGS][0],
        "team": {"id": 816, "name": None, "logo": None},
        "position": 0,
    }
    apis.responses[Endpoint.STANDINGS].append(placeholder)
    assert 816 not in {s.team.id for s in sports_io_client.get_standings()}


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeHttp:
    """requests.get stand-in: returns queued responses, records calls"""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *responses: FakeResponse
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        monkeypatch.setattr(requests, "get", self.get)

    def get(
        self, url: str, params: dict[str, Any] | None = None, **_: Any
    ) -> FakeResponse:
        self.calls.append((url, params))
        return self.responses.pop(0)


def _envelope(
    response: list[Any], errors: Any = None, paging: Any = None
) -> dict[str, Any]:
    return {"errors": errors or [], "response": response, "paging": paging}


class TestSportsIOHttp:
    def test_unwraps_the_envelope_and_adds_league_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        http = FakeHttp(monkeypatch, FakeResponse(body=_envelope([{"a": 1}])))

        assert SportsIOClient("key").get_games() == [{"a": 1}]
        url, params = http.calls[0]
        assert url.endswith("/games")
        assert params == {"league": 1, "season": SEASON}

    def test_errors_on_a_200_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeHttp(monkeypatch, FakeResponse(body=_envelope([], errors={"id": "bad"})))
        with pytest.raises(ApiDataError, match="bad"):
            SportsIOClient("key").get_team_statistics(1)

    def test_follows_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        http = FakeHttp(
            monkeypatch,
            FakeResponse(body=_envelope([1], paging={"current": 1, "total": 2})),
            FakeResponse(body=_envelope([2], paging={"current": 2, "total": 2})),
        )

        assert SportsIOClient("key").get_game_events(9) == [1, 2]
        assert http.calls[1][1] == {"id": 9, "page": 2}

    @pytest.mark.parametrize(
        ("status", "error"),
        [(429, ApiRateLimitError), (503, ApiServerError), (404, ApiDataError)],
    )
    def test_error_statuses(
        self, monkeypatch: pytest.MonkeyPatch, status: int, error: type[Exception]
    ) -> None:
        FakeHttp(monkeypatch, FakeResponse(status=status, text="nope"))
        with pytest.raises(error):
            SportsIOClient("key").get_teams()

    def test_non_json_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        FakeHttp(monkeypatch, FakeResponse(body=ValueError("not json")))
        with pytest.raises(ApiDataError, match="non-JSON"):
            SportsIOClient("key").get_teams()

    def test_backs_off_when_the_minute_quota_runs_low(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(sports_io_client.time, "sleep", sleeps.append)
        low = {"x-ratelimit-remaining": "2", "x-ratelimit-requests-remaining": "50"}
        FakeHttp(
            monkeypatch,
            FakeResponse(body=_envelope([]), headers=low),
            FakeResponse(body=_envelope([])),
        )
        client = SportsIOClient("key")

        client.get_teams()
        client.get_teams()

        assert sleeps == [sports_io_client.MINUTE_QUOTA_BACKOFF_SECONDS]

    def test_quota_carries_across_module_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the loaders call the module functions, so those must share one
        # client for the per-minute backoff to ever see the last response
        monkeypatch.setattr(sports_io_client, "get_sports_io_api", lambda: "key")
        sleeps: list[float] = []
        monkeypatch.setattr(sports_io_client.time, "sleep", sleeps.append)
        low = {"x-ratelimit-remaining": "2", "x-ratelimit-requests-remaining": "50"}
        FakeHttp(
            monkeypatch,
            FakeResponse(body=_envelope([]), headers=low),
            FakeResponse(body=_envelope([])),
        )

        sports_io_client.get_teams()
        sports_io_client.get_teams()

        assert sleeps == [sports_io_client.MINUTE_QUOTA_BACKOFF_SECONDS]


class TestOtherClientsHttp:
    def test_espn_week_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        http = FakeHttp(monkeypatch, FakeResponse(body={"events": []}))
        espn_client.EspnClient().get_scoreboard(3)
        assert http.calls[0][1] == {"seasontype": 2, "week": 3, "dates": SEASON}

    @pytest.mark.parametrize(
        ("status", "error"),
        [(429, ApiRateLimitError), (502, ApiServerError), (400, ApiDataError)],
    )
    def test_espn_errors(
        self, monkeypatch: pytest.MonkeyPatch, status: int, error: type[Exception]
    ) -> None:
        FakeHttp(monkeypatch, FakeResponse(status=status))
        with pytest.raises(error):
            espn_client.EspnClient().get_summary("1")

    def test_weather_errors_never_show_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pirate Weather's key is in the URL path
        FakeHttp(monkeypatch, FakeResponse(status=401, text="Invalid key"))
        with pytest.raises(ApiDataError) as raised:
            WeatherApiClient("secret-key-123").get_forecast(1.0, 2.0)
        assert "secret-key-123" not in str(raised.value)

    def test_weather_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        http = FakeHttp(monkeypatch, FakeResponse(body={}))
        WeatherApiClient("k").get_forecast(44.5, -88.0)
        url, params = http.calls[0]
        assert url.endswith("/k/44.5,-88.0")
        # hourly past 48h, accumulation fields, US units
        assert params is not None
        assert (params["extend"], params["version"], params["units"]) == (
            "hourly",
            "2",
            "us",
        )

    def test_odds_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        http = FakeHttp(monkeypatch, FakeResponse(body=[]))
        the_odds_api_client.TheOddsApiClient("k").get_odds()
        params = http.calls[0][1]
        assert params is not None
        assert (params["markets"], params["oddsFormat"]) == (
            "spreads,totals,h2h",
            "american",
        )

    @pytest.mark.parametrize(
        ("status", "error"),
        [(429, ApiRateLimitError), (500, ApiServerError), (401, ApiDataError)],
    )
    def test_odds_errors(
        self, monkeypatch: pytest.MonkeyPatch, status: int, error: type[Exception]
    ) -> None:
        FakeHttp(monkeypatch, FakeResponse(status=status))
        with pytest.raises(error):
            the_odds_api_client.TheOddsApiClient("k").get_odds()
