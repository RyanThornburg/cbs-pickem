"""Opt-in check that the real APIs still match what we saved and modeled -
skipped unless run with `uv run pytest -m live`. Needs the credentials in
config/.env.local and network access.

Two checks per endpoint:
1. A fresh response still validates against our pydantic models - catches
   a removed/renamed required field or a changed type.
2. Every field our models read that the saved fixture had is still in the
   fresh response wherever its parent object is - catches a renamed
   *optional* field, which validation alone can't (it would just silently
   become None from then on). Whole optional objects being absent (no
   live game means no ESPN situation) isn't counted - only fields missing
   from an object that is there.

On a failure: fix the model/loader, re-capture the fixtures
(`uv run python -m tests.fixtures.capture_api`), and run the normal suite.

Costs per run: a few Sports IO calls, 1 Odds API credit (500/month), 1
Pirate Weather call, ESPN (no quota).
"""

import types
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from api import espn_models, sports_io_models, the_odds_api_models, weather_api_models
from api.espn_client import EspnClient
from api.sports_io_client import SportsIOClient
from api.the_odds_api_client import TheOddsApiClient
from api.weather_api import WeatherApiClient
from config.config import get_sports_io_api, get_the_odds_api, get_weather_api
from tests.api_fixtures import capture_info, fixture
from tests.fixtures.capture_api import WEATHER_LOCATION

live = pytest.mark.live

type Path = tuple[str, ...]


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    """the pydantic models inside a field's type - through list[...] and
    optional/union types"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    if get_origin(annotation) in (list, Union, types.UnionType):
        return [m for arg in get_args(annotation) for m in _models_in(arg)]
    return []


def model_paths(model: type[BaseModel], prefix: Path = ()) -> set[Path]:
    """every key path (by the API's own field names) a model reads"""
    paths: set[Path] = set()
    for name, field in model.model_fields.items():
        path = (*prefix, field.alias or name)
        paths.add(path)
        for inner in _models_in(field.annotation):
            paths |= model_paths(inner, path)
    return paths


def data_paths(data: Any, prefix: Path = ()) -> set[Path]:
    """every key path present in a JSON value - list items merged"""
    paths: set[Path] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            path = (*prefix, key)
            paths.add(path)
            paths |= data_paths(value, path)
    elif isinstance(data, list):
        for item in data:
            paths |= data_paths(item, prefix)
    return paths


def missing_fields(model: type[BaseModel], saved: Any, fresh: Any) -> list[str]:
    """fields the model reads that the saved response had, and the fresh
    one doesn't, even though their parent object is there"""
    saved_paths, fresh_paths = data_paths(saved), data_paths(fresh)
    paths = model_paths(model)
    # whole objects come and go legitimately (ESPN's situation only exists
    # while a game is live) - judge the fields inside them instead
    objects = {path[:-1] for path in paths}
    return sorted(
        ".".join(path)
        for path in paths
        if path not in objects
        and path in saved_paths
        and path not in fresh_paths
        and (len(path) == 1 or path[:-1] in fresh_paths)
    )


def _check(model: type[BaseModel], saved: Any, fresh: Any) -> None:
    items = fresh if isinstance(fresh, list) else [fresh]
    for item in items:
        model.model_validate(item)
    assert missing_fields(model, saved, fresh) == []


@pytest.fixture(scope="module")
def sports_io() -> SportsIOClient:
    return SportsIOClient(get_sports_io_api())


@live
def test_sports_io_games(sports_io: SportsIOClient) -> None:
    _check(
        sports_io_models.Game, fixture("sports_io_games.json"), sports_io.get_games()
    )


@live
def test_sports_io_teams(sports_io: SportsIOClient) -> None:
    _check(
        sports_io_models.Team, fixture("sports_io_teams.json"), sports_io.get_teams()
    )


@live
def test_sports_io_standings(sports_io: SportsIOClient) -> None:
    _check(
        sports_io_models.Standing,
        [s for s in fixture("sports_io_standings.json") if s["team"]["name"]],
        sports_io.get_standings(),
    )


@live
@pytest.mark.parametrize(
    ("method", "name", "model"),
    [
        (
            "get_team_statistics",
            "sports_io_team_statistics.json",
            sports_io_models.TeamStatistics,
        ),
        ("get_game_events", "sports_io_game_events.json", sports_io_models.GameEvent),
        (
            "get_player_statistics",
            "sports_io_player_statistics.json",
            sports_io_models.TeamPlayerStatistics,
        ),
    ],
)
def test_sports_io_per_game(
    sports_io: SportsIOClient, method: str, name: str, model: type[BaseModel]
) -> None:
    fresh = getattr(sports_io, method)(capture_info()["sports_io_game_id"])
    _check(model, fixture(name), fresh)


@live
def test_espn_scoreboard() -> None:
    fresh = EspnClient().get_scoreboard(capture_info()["week"])
    _check(espn_models.Scoreboard, fixture("espn_scoreboard.json"), fresh)


@live
def test_espn_summary() -> None:
    fresh = EspnClient().get_summary(capture_info()["espn_event_id"])
    _check(espn_models.Summary, fixture("espn_summary.json"), fresh)


@live
def test_the_odds_api() -> None:
    fresh = TheOddsApiClient(get_the_odds_api()).get_odds()
    _check(the_odds_api_models.Event, fixture("the_odds_api_odds.json"), fresh)


@live
def test_pirate_weather() -> None:
    fresh = WeatherApiClient(get_weather_api()).get_forecast(*WEATHER_LOCATION)
    _check(weather_api_models.Forecast, fixture("pirate_weather_forecast.json"), fresh)


class TestDriftDetection:
    """the checker itself, on made-up data - runs with the live tests"""

    def test_renamed_optional_field_is_caught(self) -> None:
        saved = {"situation": {"yardLine": 20, "down": 1}}
        fresh = {"situation": {"yard_line": 20, "down": 1}}

        class Situation(BaseModel):
            yardLine: int | None = None
            down: int | None = None

        class Root(BaseModel):
            situation: Situation | None = None

        assert missing_fields(Root, saved, fresh) == ["situation.yardLine"]

    def test_absent_parent_is_not_drift(self) -> None:
        # no game live right now - no situation at all, which is normal
        saved = {"events": [{"situation": {"down": 1}}]}
        fresh = {"events": [{"id": "1"}]}

        class Situation(BaseModel):
            down: int | None = None

        class Event(BaseModel):
            situation: Situation | None = None

        class Root(BaseModel):
            events: list[Event]

        assert missing_fields(Root, saved, fresh) == []
