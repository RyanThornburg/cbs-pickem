"""The remaining loaders, against saved responses: CBS's side of the
schedule merged with Sports IO's (cbs_loader), espn_loader,
pregame_weather_loader and odds_loader."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from config.config import SEASON
from src import timestamps
from src.loaders import (
    cbs_loader,
    espn_loader,
    loader_helper,
    odds_loader,
    pregame_weather_loader,
    sports_io_loader,
)
from tests.api_fixtures import FakeApis, FakeCBS, capture_info
from tests.conftest import FakeD1, freeze


def _rows(
    d1: FakeD1, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]]:
    return d1.query(sql, params).results


def _count(d1: FakeD1, table: str) -> int:
    return _rows(d1, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


@pytest.fixture
def cbs(monkeypatch: pytest.MonkeyPatch) -> FakeCBS:
    return FakeCBS(monkeypatch, capture_info()["week"])


class TestCbsAndSportsIoMerge:
    """the production order: Sports IO's schedule first (the season
    fixture), then CBS's teams, weeks, games and picks on top"""

    @pytest.fixture
    def merged(self, season: FakeD1, cbs: FakeCBS) -> FakeD1:
        cbs_loader.map_cbs_to_sports_io()
        cbs_loader.load_cbs_weeks()
        cbs_loader.load_cbs_games()
        return season

    def test_teams_get_cbs_ids(self, merged: FakeD1, cbs: FakeCBS) -> None:
        mapped = _rows(
            merged,
            "SELECT abbreviation, cbs_team_id, nick_name FROM teams WHERE cbs_team_id IS NOT NULL",
        )
        cbs_teams = {
            t["cbsTeamId"]
            for e in cbs.home["poolPeriod"]["poolEvents"]
            for t in (e["homeTeam"], e["awayTeam"])
        }
        # every CBS team this week found its Sports IO row, JAC/LAR included
        assert {r["cbs_team_id"] for r in mapped} == cbs_teams
        assert all(r["nick_name"] for r in mapped)

    def test_weeks_merge_by_week_number(self, merged: FakeD1, cbs: FakeCBS) -> None:
        weeks = _rows(merged, "SELECT * FROM weeks ORDER BY week_number")
        periods = cbs.home["poolPeriods"]
        # CBS lists the weeks so far, Sports IO the saved schedule's - a
        # week both know is one row, not two
        assert [w["week_number"] for w in weeks] == sorted(
            {p["order"] for p in periods} | {3, 4, 5}
        )
        by_number = {w["week_number"]: w for w in weeks}
        for period in periods:
            week = by_number[period["order"]]
            assert week["cbs_pool_period_id"] == period["id"]
            assert week["is_current"] == period["isCurrent"]
        # Sports IO's kickoff range survives CBS's upsert
        week = by_number[capture_info()["week"]]
        assert week["start_time"] is not None
        season_name = _rows(
            merged, "SELECT name FROM seasons WHERE season_id = ?", [SEASON]
        )
        assert season_name == [{"name": cbs.home["name"]}]

    def test_games_merge_into_sports_io_rows(
        self, merged: FakeD1, cbs: FakeCBS
    ) -> None:
        week = capture_info()["week"]
        games = _rows(
            merged,
            "SELECT g.* FROM games g JOIN weeks w ON w.week_id = g.week_id WHERE w.week_number = ?",
            [week],
        )
        # one row per real game, carrying both sources' ids
        assert len(games) == len(cbs.home["poolPeriod"]["poolEvents"]) == 16
        assert all(g["sports_io_game_id"] and g["cbs_event_id"] for g in games)
        by_cbs = {g["cbs_event_id"]: g for g in games}
        for event in cbs.home["poolPeriod"]["poolEvents"]:
            game = by_cbs[event["cbsEventId"]]
            assert game["cbs_spread"] == event["homeTeamSpread"]
            assert game["game_time"] == cbs_loader._cbs_starts_at_to_iso(
                event["startsAt"]
            )
        assert _count(merged, "mapping_gaps") == 0

    def test_cbs_lagging_never_undoes_a_final(
        self, merged: FakeD1, cbs: FakeCBS
    ) -> None:
        # Sports IO has called the game; CBS still shows it in progress
        event = cbs.home["poolPeriod"]["poolEvents"][0]
        merged.query(
            "UPDATE games SET status = 'FINAL', status_desc = 'Finished', "
            "home_score = 30, away_score = 27 WHERE cbs_event_id = ?",
            [event["cbsEventId"]],
        )
        event.update(
            gameStatusDesc="INPROGRESS",
            gameStatus="P",
            homeTeamScore=24,
            awayTeamScore=27,
            homeTeamSpread=-2.5,
        )

        cbs_loader.load_cbs_games()

        (game,) = _rows(
            merged,
            "SELECT status, status_desc, home_score, away_score, cbs_spread "
            "FROM games WHERE cbs_event_id = ?",
            [event["cbsEventId"]],
        )
        assert game == {
            "status": "FINAL",
            "status_desc": "Finished",
            "home_score": 30,
            "away_score": 27,
            "cbs_spread": -2.5,  # CBS's own fields still update
        }

    def test_backfill_asks_cbs_for_that_weeks_period(
        self, merged: FakeD1, cbs: FakeCBS
    ) -> None:
        week = capture_info()["week"]
        (row,) = _rows(
            merged, "SELECT cbs_pool_period_id FROM weeks WHERE week_number = ?", [week]
        )
        cbs.periods.clear()

        cbs_loader.backfill_cbs_week(week)

        # both pages (games from pool home, picks from weekly standings)
        assert cbs.periods and set(cbs.periods) == {row["cbs_pool_period_id"]}

    def test_sports_io_after_cbs_keeps_cbs_fields(self, merged: FakeD1) -> None:
        before = _rows(
            merged,
            "SELECT game_id, cbs_event_id, cbs_spread FROM games WHERE cbs_event_id IS NOT NULL ORDER BY game_id",
        )

        sports_io_loader.load_games_data()

        after = _rows(
            merged,
            "SELECT game_id, cbs_event_id, cbs_spread FROM games WHERE cbs_event_id IS NOT NULL ORDER BY game_id",
        )
        assert after == before

    def test_picks_on_top(self, merged: FakeD1, cbs: FakeCBS) -> None:
        cbs_loader.load_cbs_users()
        cbs_loader.load_cbs_user_picks()

        entries = cbs.weekly["standings"]["weekly"]["rankedEntries"]
        assert _count(merged, "users") == len(entries)
        assert _count(merged, "weekly_performance") == len(entries)
        # every pick resolved to a real game and team
        assert _count(merged, "user_picks") > 0
        assert _count(merged, "mapping_gaps") == 0

    def test_users_reload_in_place(self, merged: FakeD1, cbs: FakeCBS) -> None:
        cbs_loader.load_cbs_users()
        cbs.members[0]["name"] = "Renamed"
        cbs_loader.load_cbs_users()

        assert _count(merged, "users") == len(cbs.members)
        renamed = _rows(
            merged, "SELECT name FROM users WHERE cbs_id = ?", [cbs.members[0]["id"]]
        )
        assert renamed == [{"name": "Renamed"}]

    def test_unknown_cbs_team_is_a_mapping_gap(
        self, season: FakeD1, cbs: FakeCBS
    ) -> None:
        cbs_loader.map_cbs_to_sports_io()
        cbs_loader.load_cbs_weeks()
        cbs.home["poolPeriod"]["poolEvents"][0]["homeTeam"]["cbsTeamId"] = 4040

        cbs_loader.load_cbs_games()

        assert _rows(season, "SELECT source, raw_value FROM mapping_gaps") == [
            {"source": "cbs", "raw_value": "4040"}
        ]

    def test_week_not_seeded_yet(self, season: FakeD1, cbs: FakeCBS) -> None:
        cbs_loader.map_cbs_to_sports_io()
        before = _rows(season, "SELECT cbs_event_id FROM games")
        cbs_loader.load_cbs_games()  # load_cbs_weeks() hasn't run
        assert _rows(season, "SELECT cbs_event_id FROM games") == before

    def test_wrong_season_on_cbs_writes_nothing(
        self, season: FakeD1, cbs: FakeCBS
    ) -> None:
        cbs.home["season"]["year"] = SEASON + 1
        cbs_loader.load_cbs_weeks()
        assert (
            _rows(
                season,
                "SELECT cbs_pool_period_id FROM weeks WHERE cbs_pool_period_id IS NOT NULL",
            )
            == []
        )


class TestEspnLoader:
    @pytest.fixture
    def weeks(self, season: FakeD1, apis: FakeApis) -> list[int]:
        saved = apis.responses["scoreboard"]
        requested: list[int] = []

        def scoreboard(week: int | None) -> dict[str, Any]:
            requested.append(week)  # type: ignore[arg-type]
            return (
                json.loads(json.dumps(saved))
                if week == capture_info()["week"]
                else {"events": []}
            )

        apis.responses["scoreboard"] = scoreboard
        self.saved = saved
        return requested

    def test_links_events_and_neutral_sites(
        self, season: FakeD1, weeks: list[int]
    ) -> None:
        espn_loader.load_espn_games()

        week = capture_info()["week"]
        games = _rows(
            season,
            "SELECT g.espn_event_id, g.neutral_site FROM games g JOIN weeks w ON w.week_id = g.week_id WHERE w.week_number = ?",
            [week],
        )
        assert all(g["espn_event_id"] for g in games)
        neutral = sum(e["competitions"][0]["neutralSite"] for e in self.saved["events"])
        assert sum(g["neutral_site"] for g in games) == neutral
        # ESPN's LAR/WSH still matched
        assert _count(season, "mapping_gaps") == 0
        assert sorted(weeks) == [3, 4, 5]

    def test_complete_weeks_are_skipped(self, season: FakeD1, weeks: list[int]) -> None:
        season.query("UPDATE weeks SET is_complete = 1 WHERE week_number = 3")
        espn_loader.load_espn_games()
        assert 3 not in weeks

        espn_loader.load_espn_games(include_complete=True)
        assert 3 in weeks

    def test_failed_week_is_skipped(self, season: FakeD1, apis: FakeApis) -> None:
        apis.responses["scoreboard"] = RuntimeError("ESPN down")
        espn_loader.load_espn_games()
        assert (
            _rows(season, "SELECT 1 FROM games WHERE espn_event_id IS NOT NULL") == []
        )

    def test_unmatched_event_is_a_mapping_gap(
        self, season: FakeD1, weeks: list[int]
    ) -> None:
        competitor = self.saved["events"][0]["competitions"][0]["competitors"][0]
        competitor["team"]["abbreviation"] = "XYZ"

        espn_loader.load_espn_games()

        (gap,) = _rows(
            season, "SELECT source, entity_type, raw_value FROM mapping_gaps"
        )
        assert (gap["source"], gap["entity_type"]) == ("espn", "team_pair")
        assert "XYZ" in gap["raw_value"]


def _forecast_rows(d1: FakeD1) -> list[dict[str, Any]]:
    return _rows(
        d1,
        "SELECT g.*, s.roof_type FROM games g JOIN stadiums s ON s.stadium_id = g.stadium_id"
        " JOIN weeks w ON w.week_id = g.week_id WHERE w.is_current = 1",
    )


class TestPregameWeather:
    @pytest.fixture
    def upcoming(self, season: FakeD1, apis: FakeApis) -> dict[str, Any]:
        """the week after the captured one, current, with the saved
        forecast's hourly entries covering its kickoffs"""
        season.query(
            "UPDATE weeks SET is_current = 1 WHERE week_number = ?",
            [capture_info()["week"] + 1],
        )
        return apis.responses["weather"]

    def test_forecast_for_each_open_air_game(
        self, season: FakeD1, apis: FakeApis, upcoming: dict[str, Any]
    ) -> None:
        pregame_weather_loader.load_pregame_weather()

        rows = _forecast_rows(season)
        outdoor = [
            r for r in rows if r["roof_type"] not in loader_helper.ENCLOSED_ROOF_TYPES
        ]
        indoor = [
            r for r in rows if r["roof_type"] in loader_helper.ENCLOSED_ROOF_TYPES
        ]
        assert outdoor and indoor
        assert apis.weather_calls == len(outdoor)  # domes never call out
        assert all(r["forecast_captured_at"] is None for r in indoor)
        hourly = {p["time"]: p for p in upcoming["hourly"]["data"]}
        for row in outdoor:
            assert row["forecast_source"] == "hourly"
            kickoff = datetime.strptime(row["game_time"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
            hour = hourly[int(kickoff.timestamp()) // 3600 * 3600]
            assert row["forecast_temp_f"] == round(hour["temperature"])
            assert row["forecast_icon"] == hour["icon"]
            hours = json.loads(row["forecast_hours_json"])
            # every hourly entry overlapping [kickoff, kickoff + window) -
            # a 00:15 kickoff touches one more hour than the window's length
            window_end = (
                kickoff.timestamp()
                + pregame_weather_loader.FORECAST_WINDOW_HOURS * 3600
            )
            expected = [
                t for t in hourly if t < window_end and t + 3600 > kickoff.timestamp()
            ]
            assert [h["time"] for h in hours] == [
                datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                for t in expected
            ]
            assert row["forecast_window_temp_f_low"] == min(h["temp_f"] for h in hours)
            assert json.loads(row["forecast_alerts_json"]) == []

    def test_only_the_current_weeks_scheduled_games(
        self, season: FakeD1, apis: FakeApis, upcoming: dict[str, Any]
    ) -> None:
        season.query(
            "UPDATE games SET status = 'IN_PROGRESS' WHERE week_id IN (SELECT week_id FROM weeks WHERE is_current = 1)"
        )
        pregame_weather_loader.load_pregame_weather()
        assert apis.weather_calls == 0

    def test_daily_fallback_past_the_hourly_horizon(
        self, season: FakeD1, upcoming: dict[str, Any]
    ) -> None:
        # an hour past the last hourly entry, still inside the last day
        kickoff_ts = upcoming["hourly"]["data"][-1]["time"] + 2 * 3600
        days = upcoming["daily"]["data"]
        day_after = next(
            d
            for d, end in zip(
                days, [d["time"] for d in days[1:]] + [days[-1]["time"] + 86400]
            )
            if d["time"] <= kickoff_ts < end
        )
        kickoff = datetime.fromtimestamp(kickoff_ts, UTC)
        outdoor = next(r for r in _forecast_rows(season) if r["roof_type"] == "Open")
        season.query(
            "UPDATE games SET game_time = ? WHERE game_id = ?",
            [kickoff.strftime("%Y-%m-%dT%H:%M:%SZ"), outdoor["game_id"]],
        )

        pregame_weather_loader.load_pregame_weather()

        row = next(
            r for r in _forecast_rows(season) if r["game_id"] == outdoor["game_id"]
        )
        assert row["forecast_source"] == "daily"
        assert row["forecast_temp_f"] is None  # no single reading for a day
        assert row["forecast_window_temp_f_high"] == round(day_after["temperatureMax"])
        assert json.loads(row["forecast_hours_json"]) == []

    def test_past_every_horizon_is_skipped(
        self, season: FakeD1, upcoming: dict[str, Any]
    ) -> None:
        outdoor = next(r for r in _forecast_rows(season) if r["roof_type"] == "Open")
        season.query(
            "UPDATE games SET game_time = '2027-01-01T18:00:00Z' WHERE game_id = ?",
            [outdoor["game_id"]],
        )

        pregame_weather_loader.load_pregame_weather()

        row = next(
            r for r in _forecast_rows(season) if r["game_id"] == outdoor["game_id"]
        )
        assert row["forecast_captured_at"] is None

    def test_alerts_are_filtered(
        self, season: FakeD1, upcoming: dict[str, Any]
    ) -> None:
        outdoor = next(r for r in _forecast_rows(season) if r["roof_type"] == "Open")
        kickoff = int(
            datetime.strptime(outdoor["game_time"], "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .timestamp()
        )

        def alert(title: str, starts: int, hours: int) -> dict[str, Any]:
            return {
                "title": title,
                "severity": "Moderate",
                "time": starts,
                "expires": starts + hours * 3600,
                "uri": f"https://alerts.example/{title}",
            }

        upcoming["alerts"] = [
            alert("Wind Advisory", kickoff - 3600, 6),  # overlaps the game
            alert(
                "Rip Current Statement", kickoff - 3600, 6
            ),  # coastal - not about the field
            alert("Flood Watch", kickoff - 86400, 2),  # over a day before kickoff
            alert("Winter Storm Warning", kickoff + 3 * 3600, 12),  # starts mid-game
        ]

        pregame_weather_loader.load_pregame_weather()

        row = next(
            r for r in _forecast_rows(season) if r["game_id"] == outdoor["game_id"]
        )
        titles = [a["title"] for a in json.loads(row["forecast_alerts_json"])]
        assert titles == ["Wind Advisory", "Winter Storm Warning"]
        first = json.loads(row["forecast_alerts_json"])[0]
        assert set(first) == {"title", "severity", "starts", "expires", "uri"}

    def test_weather_failure_skips_the_game(
        self, season: FakeD1, apis: FakeApis, upcoming: dict[str, Any]
    ) -> None:
        apis.responses["weather"] = RuntimeError("Pirate Weather down")
        pregame_weather_loader.load_pregame_weather()
        assert all(r["forecast_captured_at"] is None for r in _forecast_rows(season))

    @pytest.mark.parametrize(
        ("bearing", "compass"),
        [(0, "N"), (22, "NNE"), (90, "E"), (200, "SSW"), (350, "N")],
    )
    def test_wind_direction(self, bearing: int, compass: str) -> None:
        assert loader_helper._bearing_to_compass(bearing) == compass


class TestOdds:
    @pytest.fixture
    def before_kickoff(
        self, season: FakeD1, apis: FakeApis, monkeypatch: pytest.MonkeyPatch
    ) -> list[dict[str, Any]]:
        events = apis.responses["odds"]
        first = min(e["commence_time"] for e in events)
        now = datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        ) - timedelta(hours=1)
        freeze(monkeypatch, timestamps, now)
        return events

    def _expected_rows(self, events: list[dict[str, Any]]) -> int:
        return sum(
            1
            for e in events
            for b in e["bookmakers"]
            for m in b["markets"]
            if m["key"] in odds_loader._MARKET_MAP
        )

    def test_links_games_and_stores_every_line(
        self, season: FakeD1, before_kickoff: list[dict[str, Any]]
    ) -> None:
        odds_loader.load_the_odds_api_odds()

        linked = _rows(
            season,
            "SELECT odds_api_event_id FROM games WHERE odds_api_event_id IS NOT NULL",
        )
        assert {r["odds_api_event_id"] for r in linked} == {
            e["id"] for e in before_kickoff
        }
        assert _count(season, "odds_snapshots") == self._expected_rows(before_kickoff)
        assert _count(season, "mapping_gaps") == 0

    def test_line_values(
        self, season: FakeD1, before_kickoff: list[dict[str, Any]]
    ) -> None:
        odds_loader.load_the_odds_api_odds()

        event = before_kickoff[0]
        book = event["bookmakers"][0]
        markets = {
            m["key"]: {o["name"]: o for o in m["outcomes"]} for m in book["markets"]
        }
        rows = {
            r["market"]: r
            for r in _rows(
                season,
                "SELECT s.* FROM odds_snapshots s JOIN games g ON g.game_id = s.game_id"
                " WHERE g.odds_api_event_id = ? AND s.bookmaker = ?",
                [event["id"], book["key"]],
            )
        }
        spread = markets["spreads"]
        assert (rows["spread"]["home_point"], rows["spread"]["home_price"]) == (
            spread[event["home_team"]]["point"],
            int(spread[event["home_team"]]["price"]),
        )
        assert rows["total"]["home_point"] == markets["totals"]["Over"]["point"]
        assert rows["total"]["away_point"] == markets["totals"]["Under"]["point"]
        assert rows["moneyline"]["home_price"] == int(
            markets["h2h"][event["home_team"]]["price"]
        )
        assert rows["moneyline"]["home_point"] is None
        assert rows["spread"]["source"] == "the_odds_api"

    def test_over_is_stored_as_home(
        self, season: FakeD1, before_kickoff: list[dict[str, Any]]
    ) -> None:
        # Over and Under share a point, so only their prices tell them apart
        odds_loader.load_the_odds_api_odds()
        checked = 0
        for event in before_kickoff:
            for book in event["bookmakers"]:
                totals = next(
                    (m for m in book["markets"] if m["key"] == "totals"), None
                )
                if totals is None:
                    continue
                prices = {o["name"]: int(o["price"]) for o in totals["outcomes"]}
                if prices["Over"] == prices["Under"]:
                    continue
                (row,) = _rows(
                    season,
                    "SELECT s.home_price, s.away_price FROM odds_snapshots s"
                    " JOIN games g ON g.game_id = s.game_id"
                    " WHERE g.odds_api_event_id = ? AND s.bookmaker = ? AND s.market = 'total'",
                    [event["id"], book["key"]],
                )
                assert (row["home_price"], row["away_price"]) == (
                    prices["Over"],
                    prices["Under"],
                )
                checked += 1
        assert checked

    def test_repeat_capture_adds_nothing(
        self, season: FakeD1, before_kickoff: list[dict[str, Any]]
    ) -> None:
        odds_loader.load_the_odds_api_odds()
        count = _count(season, "odds_snapshots")
        odds_loader.load_the_odds_api_odds()
        assert _count(season, "odds_snapshots") == count

    def test_moved_line_is_a_new_row(
        self, season: FakeD1, apis: FakeApis, before_kickoff: list[dict[str, Any]]
    ) -> None:
        odds_loader.load_the_odds_api_odds()
        count = _count(season, "odds_snapshots")
        market = apis.responses["odds"][0]["bookmakers"][0]["markets"][0]
        market["last_update"] = "2030-01-01T00:00:00Z"

        odds_loader.load_the_odds_api_odds()

        assert _count(season, "odds_snapshots") == count + 1

    def test_started_games_are_skipped(
        self,
        season: FakeD1,
        before_kickoff: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = min(before_kickoff, key=lambda e: e["commence_time"])
        after_first = datetime.strptime(
            first["commence_time"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC) + timedelta(minutes=1)
        freeze(monkeypatch, timestamps, after_first)

        odds_loader.load_the_odds_api_odds()

        started = [
            e for e in before_kickoff if e["commence_time"] <= first["commence_time"]
        ]
        assert _count(season, "odds_snapshots") == self._expected_rows(
            before_kickoff
        ) - self._expected_rows(started)

    def test_unknown_team_is_a_mapping_gap(
        self, season: FakeD1, apis: FakeApis, before_kickoff: list[dict[str, Any]]
    ) -> None:
        apis.responses["odds"][0]["home_team"] = "Nowhere Nomads"

        odds_loader.load_the_odds_api_odds()

        assert _rows(season, "SELECT source, raw_value FROM mapping_gaps") == [
            {"source": "the_odds_api", "raw_value": "Nowhere Nomads"}
        ]

    def test_market_missing_a_side_is_skipped(
        self, season: FakeD1, apis: FakeApis, before_kickoff: list[dict[str, Any]]
    ) -> None:
        apis.responses["odds"][0]["bookmakers"][0]["markets"][0]["outcomes"].pop()
        odds_loader.load_the_odds_api_odds()
        assert (
            _count(season, "odds_snapshots") == self._expected_rows(before_kickoff) - 1
        )
