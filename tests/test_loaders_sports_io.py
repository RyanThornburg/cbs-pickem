"""The schedule/teams side of the loaders, against saved Sports IO
responses: sports_io_loader (weeks, games, team box scores), teams_loader,
stadiums_loader, season_loader."""

from datetime import UTC, datetime
from typing import Any

import pytest

from api.sports_io_client import Endpoint
from config.config import SEASON
from src.loaders import season_loader, sports_io_loader, stadiums_loader, teams_loader
from tests.api_fixtures import FakeApis, capture_info, fixture
from tests.conftest import Clients, FakeD1, freeze


def _rows(
    d1: FakeD1, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]]:
    return d1.query(sql, params).results


def _count(d1: FakeD1, table: str) -> int:
    return _rows(d1, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def _game(d1: FakeD1, sports_io_game_id: int) -> dict[str, Any]:
    (row,) = _rows(
        d1, "SELECT * FROM games WHERE sports_io_game_id = ?", [sports_io_game_id]
    )
    return row


def _saved_game(apis: FakeApis, sports_io_game_id: int) -> dict[str, Any]:
    return next(
        g
        for g in apis.responses[Endpoint.GAMES]
        if g["game"]["id"] == sports_io_game_id
    )


class TestHelpers:
    @pytest.mark.parametrize(
        ("short", "long", "status"),
        [
            ("NS", "Not Started", "SCHEDULED"),
            ("Q2", "Second Quarter", "IN_PROGRESS"),
            ("OT", "Overtime", "IN_PROGRESS"),
            ("HT", "Halftime", "HALFTIME"),
            ("FT", "Finished", "FINAL"),
            ("AOT", "Finished After Overtime", "FINAL"),
            ("PST", "Postponed", "POSTPONED"),
            ("CANC", "Cancelled", "CANCELLED"),
            (None, "Delayed", "DELAYED"),  # short is null during a delay
            (None, "Something New", None),
            ("ZZ", "Unknown", "ZZ"),  # passed through, with a warning
        ],
    )
    def test_status_mapping(
        self, short: str | None, long: str, status: str | None
    ) -> None:
        assert sports_io_loader._sports_io_status_to_common(short, long) == status

    @pytest.mark.parametrize(
        ("name", "week"),
        [
            ("Week 1", 1),
            ("Week 18", 18),
            ("Wild Card", None),
            ("Hall of Fame Weekend", None),
        ],
    )
    def test_regular_season_week(self, name: str, week: int | None) -> None:
        assert sports_io_loader._regular_season_week_number(name) == week

    def test_stat_strings(self) -> None:
        assert sports_io_loader._parse_made_attempted("19/34", sep="/") == (19, 34)
        assert sports_io_loader._parse_made_attempted("2-19") == (2, 19)
        assert sports_io_loader._parse_time_of_possession("32:05") == 1925
        assert (
            sports_io_loader._epoch_seconds_to_iso(1790295300) == "2026-09-25T00:15:00Z"
        )


class TestLoadGamesData:
    def test_weeks_from_kickoff_times(self, season: FakeD1) -> None:
        weeks = _rows(season, "SELECT * FROM weeks ORDER BY week_number")

        assert [w["week_number"] for w in weeks] == [3, 4, 5]
        week3 = weeks[0]
        assert (week3["name"], week3["season_id"]) == ("Week 3", SEASON)
        # first and last kickoff of the week
        kickoffs = [
            r["game_time"]
            for r in _rows(
                season,
                "SELECT game_time FROM games WHERE week_id = ?",
                [week3["week_id"]],
            )
        ]
        assert (week3["start_time"], week3["end_time"]) == (
            min(kickoffs),
            max(kickoffs),
        )

    def test_regular_season_only(self, season: FakeD1, apis: FakeApis) -> None:
        saved = apis.responses[Endpoint.GAMES]
        regular = [
            g
            for g in saved
            if g["game"]["week"].startswith("Week ")
            and g["game"]["stage"] == "Regular Season"
        ]

        assert _count(season, "games") == len(regular)
        assert len(saved) > len(regular)  # preseason and playoff games were in there
        assert _count(season, "mapping_gaps") == 0

    def test_a_final_game(self, season: FakeD1, apis: FakeApis) -> None:
        game_id = capture_info()["sports_io_game_id"]
        saved = _saved_game(apis, game_id)
        row = _game(season, game_id)

        assert row["status"] == "FINAL"
        assert row["status_desc"] == saved["game"]["status"]["long"]
        home, away = saved["scores"]["home"], saved["scores"]["away"]
        assert (row["home_score"], row["away_score"]) == (home["total"], away["total"])
        assert [row[f"home_q{q}_score"] for q in range(1, 5)] == [
            home[f"quarter_{q}"] for q in range(1, 5)
        ]
        assert [row[f"away_q{q}_score"] for q in range(1, 5)] == [
            away[f"quarter_{q}"] for q in range(1, 5)
        ]
        assert row["away_ot_score"] == away["overtime"]
        assert row["game_time"] == sports_io_loader._epoch_seconds_to_iso(
            saved["game"]["date"]["timestamp"]
        )
        teams = {
            r["team_id"]: r["sports_io_team_id"]
            for r in _rows(season, "SELECT team_id, sports_io_team_id FROM teams")
        }
        assert teams[row["home_team_id"]] == saved["teams"]["home"]["id"]
        assert row["is_complete"] == 1

    def test_scheduled_games_have_no_scores(self, season: FakeD1) -> None:
        rows = _rows(season, "SELECT * FROM games WHERE status = 'SCHEDULED'")
        assert rows
        assert all(r["home_score"] is None and r["home_q1_score"] is None for r in rows)

    def test_stadiums(self, season: FakeD1, apis: FakeApis) -> None:
        rows = _rows(
            season,
            "SELECT g.sports_io_game_id, g.is_international, s.name, s.country"
            " FROM games g LEFT JOIN stadiums s ON s.stadium_id = g.stadium_id",
        )
        by_game = {r["sports_io_game_id"]: r for r in rows}
        for saved in apis.responses[Endpoint.GAMES]:
            row = by_game.get(saved["game"]["id"])
            venue = (saved["game"].get("venue") or {}).get("name")
            if row is None or venue is None:
                continue
            expected = sports_io_loader.VENUE_NAME_CORRECTIONS.get(venue, venue)
            assert row["name"] == expected
            assert row["is_international"] == (row["country"] != "USA")
        assert any(r["is_international"] for r in rows)

    def test_stale_venue_name_is_corrected(
        self, season: FakeD1, apis: FakeApis
    ) -> None:
        reliant = [
            g["game"]["id"]
            for g in apis.responses[Endpoint.GAMES]
            if (g["game"].get("venue") or {}).get("name") == "Reliant Stadium"
            and g["game"]["stage"] == "Regular Season"
        ]
        assert reliant
        stadium = _rows(
            season,
            "SELECT s.name FROM games g JOIN stadiums s ON s.stadium_id = g.stadium_id"
            " WHERE g.sports_io_game_id = ?",
            [reliant[0]],
        )
        assert stadium == [{"name": "NRG Stadium"}]

    def test_reload_changes_nothing(self, season: FakeD1) -> None:
        before = _rows(season, "SELECT * FROM games ORDER BY game_id")
        sports_io_loader.load_games_data()
        after = _rows(season, "SELECT * FROM games ORDER BY game_id")
        assert after == before
        assert _count(season, "weeks") == 3

    def test_merges_with_a_row_cbs_created_first(
        self, season: FakeD1, apis: FakeApis
    ) -> None:
        # CBS can create the row first (no sports_io_game_id) - Sports IO must
        # fill it in, not insert a duplicate
        game_id = capture_info()["sports_io_game_id"]
        row = _game(season, game_id)
        season.query("DELETE FROM games WHERE game_id = ?", [row["game_id"]])
        season.query(
            "INSERT INTO games (week_id, home_team_id, away_team_id, cbs_event_id, cbs_spread)"
            " VALUES (?, ?, ?, 999, -3.5)",
            [row["week_id"], row["home_team_id"], row["away_team_id"]],
        )

        sports_io_loader.load_games_data()

        merged = _game(season, game_id)
        assert (merged["cbs_event_id"], merged["cbs_spread"]) == (999, -3.5)
        assert merged["home_score"] == row["home_score"]
        assert _count(season, "games") == len(
            _rows(season, "SELECT DISTINCT sports_io_game_id FROM games")
        )

    def test_null_status_keeps_the_last_known_one(
        self, season: FakeD1, apis: FakeApis
    ) -> None:
        game_id = capture_info()["sports_io_game_id"]
        saved = _saved_game(apis, game_id)
        saved["game"]["status"] = {
            "short": None,
            "long": "Something New",
            "timer": None,
        }

        sports_io_loader.load_games_data()

        assert _game(season, game_id)["status"] == "FINAL"

    def test_unknown_team_is_a_mapping_gap(
        self, season: FakeD1, apis: FakeApis
    ) -> None:
        saved = _saved_game(apis, capture_info()["sports_io_game_id"])
        saved["game"]["id"] = 777
        saved["teams"]["home"]["id"] = 4040

        sports_io_loader.load_games_data()

        gaps = _rows(season, "SELECT source, entity_type, raw_value FROM mapping_gaps")
        assert gaps == [
            {"source": "sports_io", "entity_type": "team", "raw_value": "4040"}
        ]
        assert _rows(season, "SELECT 1 FROM games WHERE sports_io_game_id = 777") == []

    def test_unknown_venue_still_loads_the_game(
        self, season: FakeD1, apis: FakeApis
    ) -> None:
        game_id = capture_info()["sports_io_game_id"]
        _saved_game(apis, game_id)["game"]["venue"] = {
            "name": "Brand New Dome",
            "city": "X",
        }

        sports_io_loader.load_games_data()

        assert _game(season, game_id)["stadium_id"] is None
        gaps = _rows(season, "SELECT entity_type, raw_value FROM mapping_gaps")
        assert gaps == [{"entity_type": "stadium", "raw_value": "Brand New Dome"}]


class TestLiveWindow:
    def test_polls_yesterday_and_today_by_date(
        self, season: FakeD1, apis: FakeApis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        freeze(monkeypatch, sports_io_loader, datetime(2026, 9, 25, 2, 0, tzinfo=UTC))
        game_id = capture_info()["sports_io_game_id"]
        saved = _saved_game(apis, game_id)
        live = {
            **saved,
            "game": {
                **saved["game"],
                "status": {"short": "Q3", "long": "Third Quarter", "timer": "8:12"},
            },
            "scores": {
                "home": {
                    **saved["scores"]["home"],
                    "total": 7,
                    "quarter_3": None,
                    "quarter_4": None,
                },
                "away": {
                    **saved["scores"]["away"],
                    "total": 17,
                    "quarter_3": None,
                    "quarter_4": None,
                },
            },
        }
        # the same game can show up under both dates - loaded once
        apis.responses[Endpoint.GAMES] = lambda params: [live]
        week_before = _rows(season, "SELECT * FROM weeks")

        sports_io_loader.load_games_data(live=True)

        dates = [params.get("date") for endpoint, params in apis.calls[-2:]]
        assert dates == ["2026-09-24", "2026-09-25"]
        row = _game(season, game_id)
        assert (row["status"], row["home_score"], row["away_score"]) == (
            "IN_PROGRESS",
            7,
            17,
        )
        assert row["home_q3_score"] is None
        # a live poll never rewrites a week's start/end
        assert _rows(season, "SELECT * FROM weeks") == week_before


class TestTeamStats:
    def test_final_box_score(self, season: FakeD1, apis: FakeApis) -> None:
        week = capture_info()["week"]
        game_id = capture_info()["sports_io_game_id"]
        saved_stats = fixture("sports_io_team_statistics.json")
        # the captured game's box score; every other game hasn't got one
        apis.responses[Endpoint.TEAM_STATISTICS] = lambda params: (
            fixture("sports_io_team_statistics.json") if params["id"] == game_id else []
        )

        loaded = sports_io_loader.load_game_statistics(week)

        internal_id = _game(season, game_id)["game_id"]
        assert loaded == {internal_id}
        rows = _rows(
            season,
            "SELECT s.*, t.sports_io_team_id FROM game_team_stats s"
            " JOIN teams t ON t.team_id = s.team_id",
        )
        assert len(rows) == 2
        for saved in saved_stats:
            row = next(r for r in rows if r["sports_io_team_id"] == saved["team"]["id"])
            stats = saved["statistics"]
            completions, attempts = stats["passing"]["comp_att"].split("/")
            assert (row["passing_completions"], row["passing_attempts"]) == (
                int(completions),
                int(attempts),
            )
            minutes, seconds = stats["posession"]["total"].split(":")
            assert row["time_of_possession_sec"] == int(minutes) * 60 + int(seconds)
            assert row["yards_total"] == stats["yards"]["total"]
            assert row["yards_per_play"] == float(stats["yards"]["yards_per_play"])

    def test_unknown_week(self, season: FakeD1, apis: FakeApis) -> None:
        assert sports_io_loader.load_game_statistics(17) == set()
        assert not any(e == Endpoint.TEAM_STATISTICS for e, _ in apis.calls)

    # DELAYED too, so stats keep polling through a weather delay
    @pytest.mark.parametrize("status", ["HALFTIME", "DELAYED"])
    def test_live_games_only(
        self, season: FakeD1, apis: FakeApis, status: str
    ) -> None:
        game_id = capture_info()["sports_io_game_id"]
        season.query(
            "UPDATE games SET status = ? WHERE sports_io_game_id = ?",
            [status, game_id],
        )

        loaded = sports_io_loader.load_live_game_statistics()

        stats_calls = [p["id"] for e, p in apis.calls if e == Endpoint.TEAM_STATISTICS]
        assert stats_calls == [game_id]
        assert loaded == {_game(season, game_id)["game_id"]}

    def test_no_live_games(self, season: FakeD1, apis: FakeApis) -> None:
        assert sports_io_loader.load_live_game_statistics() == set()

    def test_unknown_team_is_a_mapping_gap(
        self, season: FakeD1, apis: FakeApis
    ) -> None:
        game_id = capture_info()["sports_io_game_id"]
        season.query(
            "UPDATE games SET status = 'IN_PROGRESS' WHERE sports_io_game_id = ?",
            [game_id],
        )
        apis.responses[Endpoint.TEAM_STATISTICS][0]["team"]["id"] = 4040

        sports_io_loader.load_live_game_statistics()

        assert _count(season, "game_team_stats") == 1
        assert _rows(season, "SELECT raw_value FROM mapping_gaps") == [
            {"raw_value": "4040"}
        ]


class TestTeams:
    def test_teams_with_standings(self, season: FakeD1, apis: FakeApis) -> None:
        rows = _rows(season, "SELECT * FROM teams")
        assert len(rows) == 32
        standings = {s["team"]["id"]: s for s in apis.responses[Endpoint.STANDINGS]}
        for row in rows:
            standing = standings[row["sports_io_team_id"]]
            assert (row["conference"], row["division"]) == (
                standing["conference"],
                standing["division"],
            )
            assert (row["wins"], row["losses"], row["ties"]) == (
                standing["won"],
                standing["lost"],
                standing["ties"],
            )
            assert row["season"] == SEASON

    def test_daily_rerun_updates_records(self, season: FakeD1, apis: FakeApis) -> None:
        standing = apis.responses[Endpoint.STANDINGS][0]
        standing["won"] += 1

        teams_loader.main()

        row = _rows(
            season,
            "SELECT wins FROM teams WHERE sports_io_team_id = ?",
            [standing["team"]["id"]],
        )
        assert row == [{"wins": standing["won"]}]
        assert _count(season, "teams") == 32

    def test_team_without_an_abbreviation_is_skipped(
        self, apis: FakeApis, loaders: Clients
    ) -> None:
        apis.responses[Endpoint.TEAMS][0]["code"] = None
        teams_loader.main()
        assert _count(loaders.d1, "teams") == 31

    def test_team_missing_from_standings(
        self, apis: FakeApis, loaders: Clients
    ) -> None:
        dropped = apis.responses[Endpoint.STANDINGS].pop()
        teams_loader.main()
        row = _rows(
            loaders.d1,
            "SELECT division, wins FROM teams WHERE sports_io_team_id = ?",
            [dropped["team"]["id"]],
        )
        assert row == [{"division": None, "wins": None}]


class TestStadiumsAndSeason:
    def test_stadiums_seed(self, loaders: Clients) -> None:
        stadiums_loader.load_stadiums()
        stadiums_loader.load_stadiums()  # re-run updates in place

        expected = stadiums_loader.STADIUMS + stadiums_loader.INTERNATIONAL_VENUES
        assert _count(loaders.d1, "stadiums") == len(expected)
        missing_coords = _rows(
            loaders.d1,
            "SELECT name FROM stadiums WHERE latitude IS NULL OR longitude IS NULL",
        )
        assert missing_coords == []  # pregame/live weather needs them

    def test_season_loader(self, apis: FakeApis, loaders: Clients) -> None:
        apis.responses[Endpoint.LEAGUES] = [
            {
                "league": {"id": 1, "name": "NFL"},
                "seasons": [
                    {
                        "year": SEASON,
                        "start": "2026-07-30",
                        "end": "2027-02-14",
                        "current": True,
                    }
                ],
            }
        ]
        loaders.d1.query(
            "INSERT INTO seasons (season_id, is_active) VALUES (?, 1)", [SEASON - 1]
        )

        season_loader.main()

        rows = _rows(
            loaders.d1,
            "SELECT season_id, name, is_active FROM seasons ORDER BY season_id",
        )
        assert rows == [
            {"season_id": SEASON - 1, "name": None, "is_active": 0},
            {"season_id": SEASON, "name": f"{SEASON} Season", "is_active": 1},
        ]

    def test_season_loader_with_no_current_season(
        self, apis: FakeApis, loaders: Clients
    ) -> None:
        apis.responses[Endpoint.LEAGUES] = []
        season_loader.main()
        assert _count(loaders.d1, "seasons") == 0
