"""Save fresh raw responses from every external API the loaders read, as
test fixtures under tests/fixtures/api/ - the same unvalidated JSON the
clients hand to their pydantic models, so tests exercise real parsing.

Re-run after an upstream API changes shape (tests/test_api_live.py is
what notices), then re-run the suite to see what broke.

Costs: about 8 Sports IO calls, 1 of The Odds API's monthly credits, 1
Pirate Weather call, a few ESPN calls (no quota). Nothing here holds
anyone's personal data or an API key.

Usage: uv run python -m tests.fixtures.capture_api
"""

import json
from pathlib import Path
from typing import Any

from api.espn_client import ABBREV_CORRECTIONS as ESPN_ABBREV_CORRECTIONS
from api.espn_client import EspnClient
from api.sports_io_client import Endpoint, SportsIOClient
from api.the_odds_api_client import TheOddsApiClient
from api.weather_api import WeatherApiClient
from config.config import get_sports_io_api, get_the_odds_api, get_weather_api

FIXTURE_DIR = Path(__file__).parent / "api"

# Lambeau Field - open air, so a forecast always has real weather in it
WEATHER_LOCATION = (44.5013, -88.0622)

_FINAL = {"FT", "AOT"}


# only the parts of ESPN's summary the models read - the rest (box score,
# leaders, odds, news, ...) is most of its ~800KB
_SUMMARY_KEYS = ("winprobability", "drives")


def save(name: str, data: Any) -> None:
    path = FIXTURE_DIR / name
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {path} ({path.stat().st_size // 1024} KB)")


def _week_number(game: dict[str, Any]) -> int | None:
    week = game["game"]["week"]
    if game["game"]["stage"] != "Regular Season" or not week.startswith("Week "):
        return None
    return int(week.removeprefix("Week "))


def _trim_games(games: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """a few preseason and playoff games (to test they're skipped), plus
    every game in the latest complete week and the two after it - all a
    loader test needs, at a fraction of the season's size. Also returns
    the latest complete week."""
    weeks: dict[int, list[dict[str, Any]]] = {}
    for game in games:
        week = _week_number(game)
        if week is not None:
            weeks.setdefault(week, []).append(game)
    complete = [
        week
        for week, week_games in weeks.items()
        if all(g["game"]["status"]["short"] in _FINAL for g in week_games)
    ]
    latest = max(complete)
    kept_weeks = {latest, latest + 1, latest + 2}
    preseason = [g for g in games if g["game"]["stage"] == "Pre Season"][:3]
    playoffs = [
        g
        for g in games
        if g["game"]["stage"] != "Pre Season" and _week_number(g) is None
    ][:3]
    regular = [g for g in games if _week_number(g) in kept_weeks]
    return preseason + regular + playoffs, latest


def main() -> None:
    FIXTURE_DIR.mkdir(exist_ok=True)

    sports_io = SportsIOClient(get_sports_io_api())
    games, week = _trim_games(sports_io.get_games())
    save("sports_io_games.json", games)
    save("sports_io_teams.json", sports_io.get_teams())
    # raw, placeholder row included - get_standings() is what drops it
    save("sports_io_standings.json", sports_io._request(Endpoint.STANDINGS))

    final = next(
        g
        for g in games
        if _week_number(g) == week and g["game"]["status"]["short"] in _FINAL
    )
    game_id = final["game"]["id"]
    save("sports_io_team_statistics.json", sports_io.get_team_statistics(game_id))
    save("sports_io_game_events.json", sports_io.get_game_events(game_id))
    save("sports_io_player_statistics.json", sports_io.get_player_statistics(game_id))

    espn = EspnClient()
    scoreboard = espn.get_scoreboard(week)
    save("espn_scoreboard.json", scoreboard)
    # the same game on ESPN, for its win probability curve
    codes = {t["id"]: t["code"] for t in sports_io.get_teams()}
    home_code = codes[final["teams"]["home"]["id"]]
    event_id = next(
        event["id"]
        for event in scoreboard["events"]
        for competitor in event["competitions"][0]["competitors"]
        if competitor["homeAway"] == "home"
        and ESPN_ABBREV_CORRECTIONS.get(
            competitor["team"]["abbreviation"], competitor["team"]["abbreviation"]
        )
        == home_code
    )
    summary = espn.get_summary(event_id)
    save("espn_summary.json", {key: summary[key] for key in _SUMMARY_KEYS})

    save("the_odds_api_odds.json", TheOddsApiClient(get_the_odds_api()).get_odds())
    save(
        "pirate_weather_forecast.json",
        WeatherApiClient(get_weather_api()).get_forecast(*WEATHER_LOCATION),
    )
    save(
        "capture.json",
        {"week": week, "sports_io_game_id": game_id, "espn_event_id": event_id},
    )


if __name__ == "__main__":
    main()
