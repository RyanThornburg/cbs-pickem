"""The game-day loaders, against saved ESPN/Sports IO/Pirate Weather
responses: game_snapshots_loader (what the live scoreboard shows),
scoring_plays_loader, player_stats_loader, win_probability_loader."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from api.sports_io_client import Endpoint
from src.loaders import (
    game_snapshots_loader,
    player_stats_loader,
    scoring_plays_loader,
    win_probability_loader,
)
from src.timestamps import utc_iso
from tests.api_fixtures import FakeApis, capture_info, fixture
from tests.conftest import FakeD1, freeze


def _rows(
    d1: FakeD1, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]]:
    return d1.query(sql, params).results


def _count(d1: FakeD1, table: str) -> int:
    return _rows(d1, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def _captured_game(d1: FakeD1) -> dict[str, Any]:
    """the game every per-game fixture was captured for"""
    (row,) = _rows(
        d1,
        "SELECT g.*, ht.abbreviation AS home_abbr, at.abbreviation AS away_abbr,"
        " s.roof_type FROM games g"
        " JOIN teams ht ON ht.team_id = g.home_team_id"
        " JOIN teams at ON at.team_id = g.away_team_id"
        " LEFT JOIN stadiums s ON s.stadium_id = g.stadium_id"
        " WHERE g.sports_io_game_id = ?",
        [capture_info()["sports_io_game_id"]],
    )
    return row


def _kickoff(game: dict[str, Any]) -> datetime:
    return datetime.strptime(game["game_time"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )


def _espn_event(apis: FakeApis, home_abbr: str) -> dict[str, Any]:
    return next(
        e
        for e in apis.responses["scoreboard"]["events"]
        if any(
            c["homeAway"] == "home" and c["team"]["abbreviation"] == home_abbr
            for c in e["competitions"][0]["competitors"]
        )
    )


def make_live(
    event: dict[str, Any],
    period: int = 2,
    clock: str = "3:15",
    score: tuple[int, int] = (7, 10),
    play_id: str = "p1",
    situation: dict[str, Any] | None = None,
) -> None:
    """turn a finished saved ESPN event back into one in progress - the
    situation block uses ESPN's own key names (api/espn_models.py)"""
    competition = event["competitions"][0]
    competition["status"].update(
        period=period,
        displayClock=clock,
        type={
            **competition["status"]["type"],
            "name": "STATUS_IN_PROGRESS",
            "state": "in",
            "completed": False,
        },
    )
    by_side = {c["homeAway"]: c for c in competition["competitors"]}
    by_side["home"]["score"], by_side["away"]["score"] = str(score[0]), str(score[1])
    competition["situation"] = (
        situation
        if situation is not None
        else {
            "down": 3,
            "distance": 7,
            "yardLine": 18,
            "downDistanceText": "3rd & 7 at ATL 18",
            "possessionText": "ATL 18",
            "isRedZone": True,
            "homeTimeouts": 2,
            "awayTimeouts": 3,
            "possession": by_side["home"]["team"]["id"],
            "lastPlay": {
                "id": play_id,
                "text": " Pass complete for 9 yards",
                "type": {"text": "Pass Reception"},
                "probability": {"homeWinPercentage": 0.714, "awayWinPercentage": 0.286},
                "drive": {
                    "description": "6 plays, 55 yards, 3:10",
                    "start": {"yardLine": 27, "text": "GB 27"},
                },
            },
        }
    )


class Live:
    """the captured game, in progress on ESPN, with the clock frozen an
    hour after its kickoff"""

    def __init__(
        self, d1: FakeD1, apis: FakeApis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.d1 = d1
        self.apis = apis
        self.game = _captured_game(d1)
        d1.query(
            "UPDATE games SET status = 'IN_PROGRESS' WHERE game_id = ?",
            [self.game["game_id"]],
        )
        self.event = _espn_event(apis, self.game["home_abbr"])
        make_live(self.event)
        self.now = _kickoff(self.game) + timedelta(hours=1)
        freeze(monkeypatch, game_snapshots_loader, self.now)

    def snapshots(self) -> list[dict[str, Any]]:
        return _rows(self.d1, "SELECT * FROM game_snapshots ORDER BY snapshot_id")


@pytest.fixture
def live(season: FakeD1, apis: FakeApis, monkeypatch: pytest.MonkeyPatch) -> Live:
    return Live(season, apis, monkeypatch)


class TestGameSnapshots:
    def test_snapshot_of_a_live_game(self, live: Live) -> None:
        captured = game_snapshots_loader.load_game_snapshots()

        assert captured == {live.game["game_id"]}
        (snap,) = live.snapshots()
        assert (snap["quarter"], snap["time_remaining"]) == (2, "3:15")
        assert snap["status_desc"] == "STATUS_IN_PROGRESS"
        assert (snap["home_score"], snap["away_score"], snap["possession"]) == (
            7,
            10,
            "HOME",
        )
        assert (snap["down"], snap["distance"], snap["yard_line"]) == (3, 7, 18)
        assert snap["is_red_zone"] == 1
        assert snap["last_play_text"] == "Pass complete for 9 yards"  # ESPN pads it
        assert snap["last_play_type"] == "Pass Reception"
        assert (snap["drive_start_yard_line"], snap["drive_start_text"]) == (
            27,
            "GB 27",
        )
        assert (snap["home_win_pct"], snap["away_win_pct"]) == (71.4, 28.6)

    def test_links_the_espn_event_on_first_sight(self, live: Live) -> None:
        game_snapshots_loader.load_game_snapshots()
        row = _rows(
            live.d1,
            "SELECT espn_event_id FROM games WHERE game_id = ?",
            [live.game["game_id"]],
        )
        assert row == [{"espn_event_id": live.event["id"]}]

    def test_weather_at_an_open_air_stadium(self, live: Live) -> None:
        assert live.game["roof_type"] == "Open"

        game_snapshots_loader.load_game_snapshots()

        (snap,) = live.snapshots()
        current = live.apis.responses["weather"]["currently"]
        assert snap["temperature_f"] == round(current["temperature"])
        assert snap["weather_icon"] == current["icon"]
        assert json.loads(snap["weather_alerts_json"]) == []
        # the same ISO8601 UTC text as every other timestamp
        assert snap["weather_captured_at"] == utc_iso(live.now)

    def test_nothing_changed_means_no_row(self, live: Live) -> None:
        game_snapshots_loader.load_game_snapshots()
        assert game_snapshots_loader.load_game_snapshots() == set()
        assert len(live.snapshots()) == 1

    def test_new_play_adds_a_row_and_reuses_recent_weather(self, live: Live) -> None:
        game_snapshots_loader.load_game_snapshots()
        make_live(live.event, clock="2:40", play_id="p2")

        assert game_snapshots_loader.load_game_snapshots() == {live.game["game_id"]}

        first, second = live.snapshots()
        assert second["time_remaining"] == "2:40"
        # weather is carried forward rather than fetched every 15 seconds
        assert live.apis.weather_calls == 1
        assert second["weather_captured_at"] == first["weather_captured_at"]
        assert second["temperature_f"] == first["temperature_f"]

    def test_reuses_weather_stamped_in_the_old_format(self, live: Live) -> None:
        # rows before 2026-09-29 stored "YYYY-MM-DD HH:MM:SS"
        game_snapshots_loader.load_game_snapshots()
        old_format = (live.now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        live.d1.query("UPDATE game_snapshots SET weather_captured_at = ?", [old_format])
        make_live(live.event, clock="2:40", play_id="p2")

        game_snapshots_loader.load_game_snapshots()

        assert live.apis.weather_calls == 1
        assert live.snapshots()[-1]["weather_captured_at"] == old_format

    def test_same_clock_new_play_still_counts(self, live: Live) -> None:
        # e.g. a penalty with no time off the clock
        game_snapshots_loader.load_game_snapshots()
        make_live(live.event, play_id="p2")
        assert game_snapshots_loader.load_game_snapshots() == {live.game["game_id"]}

    def test_no_situation_between_quarters(self, live: Live) -> None:
        make_live(live.event, situation={})
        live.event["competitions"][0].pop("situation")

        game_snapshots_loader.load_game_snapshots()

        (snap,) = live.snapshots()
        assert snap["quarter"] == 2
        assert (snap["down"], snap["possession"], snap["last_play_id"]) == (
            None,
            None,
            None,
        )
        assert snap["is_red_zone"] is None

    def test_enclosed_stadium_skips_weather(self, live: Live) -> None:
        live.d1.query(
            "UPDATE stadiums SET roof_type = 'Dome' WHERE stadium_id = ?",
            [live.game["stadium_id"]],
        )

        game_snapshots_loader.load_game_snapshots()

        (snap,) = live.snapshots()
        assert snap["temperature_f"] is None
        assert snap["weather_captured_at"] is None
        assert live.apis.weather_calls == 0

    def test_weather_failure_still_snapshots(self, live: Live) -> None:
        live.apis.responses["weather"] = RuntimeError("Pirate Weather down")

        game_snapshots_loader.load_game_snapshots()

        (snap,) = live.snapshots()
        assert snap["quarter"] == 2
        assert snap["temperature_f"] is None

    def test_espn_failure_is_not_fatal(self, live: Live) -> None:
        live.apis.responses["scoreboard"] = RuntimeError("ESPN down")
        assert game_snapshots_loader.load_game_snapshots() == set()
        assert live.snapshots() == []

    def test_finished_on_espn_is_not_snapshotted(
        self, live: Live, apis: FakeApis
    ) -> None:
        live.event["competitions"][0]["status"]["type"]["state"] = "post"
        assert game_snapshots_loader.load_game_snapshots() == set()

    def test_unmatched_game_is_a_mapping_gap(self, live: Live) -> None:
        live.apis.responses["scoreboard"]["events"].remove(live.event)

        game_snapshots_loader.load_game_snapshots()

        gaps = _rows(live.d1, "SELECT source, entity_type, raw_value FROM mapping_gaps")
        assert gaps == [
            {
                "source": "espn",
                "entity_type": "team_pair",
                "raw_value": f"{live.game['away_abbr']}@{live.game['home_abbr']}",
            }
        ]

    def test_espn_abbreviation_corrections(self, live: Live) -> None:
        # ESPN's LAR/WSH are LA/WAS in teams.abbreviation
        competitor = next(
            c
            for c in live.event["competitions"][0]["competitors"]
            if c["homeAway"] == "home"
        )
        live.d1.query("UPDATE teams SET abbreviation = 'LA_' WHERE abbreviation = 'LA'")
        live.d1.query(
            "UPDATE teams SET abbreviation = 'LA' WHERE team_id = ?",
            [live.game["home_team_id"]],
        )
        competitor["team"]["abbreviation"] = "LAR"

        assert game_snapshots_loader.load_game_snapshots() == {live.game["game_id"]}

    def test_outside_game_time(
        self, season: FakeD1, apis: FakeApis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        freeze(
            monkeypatch, game_snapshots_loader, datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        )
        apis.responses["scoreboard"] = RuntimeError("should not be called")

        assert game_snapshots_loader.has_candidate_games() is False
        assert game_snapshots_loader.load_game_snapshots() == set()

    def test_candidate_window(self, live: Live) -> None:
        assert game_snapshots_loader.has_candidate_games() is True


def _plays(d1: FakeD1, game_id: int) -> list[dict[str, Any]]:
    return _rows(
        d1,
        "SELECT * FROM game_scoring_plays WHERE game_id = ? ORDER BY sequence",
        [game_id],
    )


class TestScoringPlays:
    @pytest.fixture
    def final(self, season: FakeD1, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        game = _captured_game(season)
        freeze(monkeypatch, scoring_plays_loader, _kickoff(game) + timedelta(hours=4))
        return game

    def _event_calls(self, apis: FakeApis, game_id: int | None = None) -> int:
        """Sports IO events fetches - for one game (by its Sports IO id),
        or all of them. The rest of the saved week is FINAL too, and with
        the clock frozen on Thursday night those look recent as well."""
        return sum(
            1
            for endpoint, params in apis.calls
            if endpoint == Endpoint.GAME_EVENTS
            and (game_id is None or params["id"] == game_id)
        )

    def test_loads_a_game_whose_score_moved(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        scoring_plays_loader.load_scoring_plays()

        plays = _plays(season, final["game_id"])
        saved = apis.responses[Endpoint.GAME_EVENTS]
        assert [p["sequence"] for p in plays] == list(range(1, len(saved) + 1))
        quarters = {"First": 1, "Second": 2, "Third": 3, "Fourth": 4, "Overtime": 5}
        assert [p["quarter"] for p in plays] == [quarters[e["quarter"]] for e in saved]
        assert [p["type"] for p in plays] == [e["type"] for e in saved]
        assert all(p["team_id"] is not None for p in plays)
        # the last play lands on the final score
        assert (plays[-1]["home_score"], plays[-1]["away_score"]) == (
            final["home_score"],
            final["away_score"],
        )

    def test_caught_up_game_is_not_fetched_again(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        scoring_plays_loader.load_scoring_plays()
        game_id = final["sports_io_game_id"]
        assert self._event_calls(apis, game_id) == 1

        scoring_plays_loader.load_scoring_plays()

        assert self._event_calls(apis, game_id) == 1

    def test_new_score_refetches(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        scoring_plays_loader.load_scoring_plays()
        season.query(
            "UPDATE games SET home_score = home_score + 7 WHERE game_id = ?",
            [final["game_id"]],
        )
        game_id = final["sports_io_game_id"]

        scoring_plays_loader.load_scoring_plays()

        assert self._event_calls(apis, game_id) == 2

    def test_long_finished_game_is_left_alone(
        self, season: FakeD1, apis: FakeApis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        game = _captured_game(season)
        freeze(monkeypatch, scoring_plays_loader, _kickoff(game) + timedelta(days=2))

        scoring_plays_loader.load_scoring_plays()

        assert self._event_calls(apis, game["sports_io_game_id"]) == 0

    def test_empty_response_keeps_stored_plays(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        scoring_plays_loader.load_scoring_plays()
        stored = _plays(season, final["game_id"])
        season.query(
            "UPDATE games SET home_score = home_score + 3 WHERE game_id = ?",
            [final["game_id"]],
        )
        apis.responses[Endpoint.GAME_EVENTS] = []

        scoring_plays_loader.load_scoring_plays()

        assert _plays(season, final["game_id"]) == stored

    def test_fetch_failure_is_skipped(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        def fail(params: dict[str, Any]) -> list[Any]:
            raise RuntimeError("Sports IO down")

        apis.responses[Endpoint.GAME_EVENTS] = fail
        scoring_plays_loader.load_scoring_plays()
        assert _plays(season, final["game_id"]) == []

    def test_unknown_quarter_is_a_mapping_gap(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        apis.responses[Endpoint.GAME_EVENTS][0]["quarter"] = "Fifth"

        scoring_plays_loader.load_scoring_plays()

        assert _plays(season, final["game_id"])[0]["quarter"] is None
        assert _rows(season, "SELECT entity_type, raw_value FROM mapping_gaps") == [
            {"entity_type": "event_quarter", "raw_value": "Fifth"}
        ]

    def test_week_backfill_ignores_the_score_check(
        self, season: FakeD1, apis: FakeApis
    ) -> None:
        # no clock freeze - the game is long past the recheck window
        scoring_plays_loader.backfill_week_scoring_plays(capture_info()["week"])
        game = _captured_game(season)
        assert _plays(season, game["game_id"])
        assert self._event_calls(apis) == 16  # every game that week


def _player_rows(d1: FakeD1) -> dict[tuple[int, str, str], dict[str, Any]]:
    return {
        (r["team_id"], r["stat_group"], r["player_name"]): {
            **r,
            "stats": json.loads(r["stats_json"]),
        }
        for r in _rows(d1, "SELECT * FROM game_player_stats")
    }


class TestPlayerStats:
    @pytest.fixture
    def week(self, season: FakeD1, apis: FakeApis) -> int:
        game_id = capture_info()["sports_io_game_id"]
        saved = apis.responses[Endpoint.PLAYER_STATISTICS]
        # only the captured game has a box score saved
        apis.responses[Endpoint.PLAYER_STATISTICS] = lambda params: (
            json.loads(json.dumps(saved)) if params["id"] == game_id else []
        )
        self.saved = saved
        return capture_info()["week"]

    def test_week_load(self, season: FakeD1, week: int) -> None:
        changed = player_stats_loader.load_week_player_stats(week)

        game = _captured_game(season)
        assert changed == {game["game_id"]}
        rows = _player_rows(season)
        expected = sum(
            1
            for team in self.saved
            for group in team["groups"]
            for line in group["players"]
            if line["player"]["name"]
        )
        assert len(rows) == expected
        assert {r["game_id"] for r in rows.values()} == {game["game_id"]}

    def test_stat_values(self, season: FakeD1, week: int) -> None:
        player_stats_loader.load_week_player_stats(week)

        passing = next(
            r for r in _player_rows(season).values() if r["stat_group"] == "Passing"
        )
        stats = passing["stats"]
        assert isinstance(stats["yards"], int)
        assert isinstance(stats["comp_att"], str) and "/" in stats["comp_att"]
        # keys are snake_cased from Sports IO's "passing touch downs" style
        assert all(key == key.lower() and " " not in key for key in stats)

    @pytest.mark.parametrize(
        ("raw", "value"),
        [
            ("292", 292),
            ("-3", -3),
            ("8.6", 8.6),
            ("19/34", "19/34"),
            ("2-19", "2-19"),
            (None, None),
        ],
    )
    def test_stat_value(self, raw: str | None, value: Any) -> None:
        assert player_stats_loader._stat_value(raw) == value

    def test_rerun_with_nothing_new(self, season: FakeD1, week: int) -> None:
        player_stats_loader.load_week_player_stats(week)
        assert player_stats_loader.load_week_player_stats(week) == set()

    def test_a_changed_line_and_a_dropped_one(self, season: FakeD1, week: int) -> None:
        player_stats_loader.load_week_player_stats(week)
        receiving = next(g for g in self.saved[0]["groups"] if g["name"] == "Receiving")
        changed_line, dropped_line = receiving["players"][0], receiving["players"].pop()
        yards = next(s for s in changed_line["statistics"] if s["name"] == "yards")
        yards["value"] = "999"

        assert player_stats_loader.load_week_player_stats(week) == {
            _captured_game(season)["game_id"]
        }

        rows = _player_rows(season)
        names = {name for (_, group, name) in rows if group == "Receiving"}
        assert changed_line["player"]["name"] in names
        assert dropped_line["player"]["name"] not in names
        changed = next(
            r
            for r in rows.values()
            if r["player_name"] == changed_line["player"]["name"]
            and r["stat_group"] == "Receiving"
        )
        assert changed["stats"]["yards"] == 999

    def test_empty_response_never_wipes(
        self, season: FakeD1, apis: FakeApis, week: int
    ) -> None:
        player_stats_loader.load_week_player_stats(week)
        before = _count(season, "game_player_stats")
        apis.responses[Endpoint.PLAYER_STATISTICS] = []

        player_stats_loader.load_week_player_stats(week)

        assert _count(season, "game_player_stats") == before

    # DELAYED too, so stats keep polling through a weather delay
    @pytest.mark.parametrize("status", ["IN_PROGRESS", "DELAYED"])
    def test_live_games_only(
        self, season: FakeD1, apis: FakeApis, week: int, status: str
    ) -> None:
        assert player_stats_loader.load_live_player_stats() == set()
        game = _captured_game(season)
        season.query(
            "UPDATE games SET status = ? WHERE game_id = ?",
            [status, game["game_id"]],
        )

        assert player_stats_loader.load_live_player_stats() == {game["game_id"]}

    def test_unmapped_team_is_a_gap_and_keeps_its_rows(
        self, season: FakeD1, week: int
    ) -> None:
        player_stats_loader.load_week_player_stats(week)
        before = _count(season, "game_player_stats")
        self.saved[0]["team"]["id"] = 4040

        player_stats_loader.load_week_player_stats(week)

        assert _count(season, "game_player_stats") == before
        assert _rows(season, "SELECT raw_value FROM mapping_gaps") == [
            {"raw_value": "4040"}
        ]


class TestWinProbability:
    @pytest.fixture
    def final(self, season: FakeD1, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        game = _captured_game(season)
        season.query(
            "UPDATE games SET espn_event_id = ? WHERE game_id = ?",
            [capture_info()["espn_event_id"], game["game_id"]],
        )
        freeze(monkeypatch, win_probability_loader, _kickoff(game) + timedelta(hours=5))
        return game

    def _curve(self, d1: FakeD1, game_id: int) -> list[dict[str, Any]]:
        row = _rows(
            d1,
            "SELECT points_json FROM game_win_probability WHERE game_id = ?",
            [game_id],
        )
        return json.loads(row[0]["points_json"]) if row else []

    def test_curve_for_a_final_game(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        stored = win_probability_loader.load_final_win_probability()

        assert stored == {final["game_id"]}
        curve = self._curve(season, final["game_id"])
        assert len(curve) == len(apis.responses["summary"]["winprobability"])
        # ESPN's one pre-kickoff point matches no play
        pre = [p for p in curve if p["period"] == 0]
        assert len(pre) == 1
        assert (pre[0]["home_score"], pre[0]["away_score"], pre[0]["clock"]) == (
            0,
            0,
            None,
        )
        # ESPN's fractions as 0-100 percents
        saved = apis.responses["summary"]["winprobability"]
        assert [p["home_win_pct"] for p in curve] == [
            round(point["homeWinPercentage"] * 100, 1) for point in saved
        ]
        assert all(1 <= p["period"] <= 5 for p in curve if p is not pre[0])
        assert any(p["scoring_play"] for p in curve)
        # ends on the final score
        assert (curve[-1]["home_score"], curve[-1]["away_score"]) == (
            final["home_score"],
            final["away_score"],
        )

    def test_fetched_once(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        win_probability_loader.load_final_win_probability()
        assert win_probability_loader.load_final_win_probability() == set()
        assert apis.summary_calls == [capture_info()["espn_event_id"]]

    def test_failure_retries_later(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        apis.responses["summary"] = RuntimeError("ESPN down")
        assert win_probability_loader.load_final_win_probability() == set()

        apis.responses["summary"] = fixture("espn_summary.json")
        assert win_probability_loader.load_final_win_probability() == {final["game_id"]}

    def test_no_curve_yet(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        apis.responses["summary"] = {"winprobability": [], "drives": {"previous": []}}
        assert win_probability_loader.load_final_win_probability() == set()

    def test_gives_up_after_the_retry_window(
        self,
        season: FakeD1,
        apis: FakeApis,
        final: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        freeze(monkeypatch, win_probability_loader, _kickoff(final) + timedelta(days=4))
        assert win_probability_loader.load_final_win_probability() == set()
        assert apis.summary_calls == []

    def test_week_backfill_replaces(
        self, season: FakeD1, apis: FakeApis, final: dict[str, Any]
    ) -> None:
        season.query(
            "INSERT INTO game_win_probability (game_id, points_json) VALUES (?, '[]')",
            [final["game_id"]],
        )
        win_probability_loader.backfill_week_win_probability(capture_info()["week"])
        assert self._curve(season, final["game_id"])
