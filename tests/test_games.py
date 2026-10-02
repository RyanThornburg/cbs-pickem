"""The scoreboard's KV keys: week:{season}:{weekNN}:games
(src/kv_writer/games.py) and game:{season}:{game_id}:details
(src/kv_writer/game_details.py). The web UI reads both, so the contract
tests pin their field names - a rename or a dropped field should fail here,
not on the live scoreboard."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from config.config import SEASON
from src.kv_writer import game_details, games
from tests.conftest import Clients, FakeD1, Seed, iso

KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)

GAME_FIELDS = {
    "game_id",
    "home_team",
    "away_team",
    "status",
    "status_desc",
    "home_score",
    "away_score",
    "linescore",
    "leaders",
    "scoring_plays",
    "game_time",
    "cbs_spread",
    "tv_network",
    "gametracker_url",
    "neutral_site",
    "stadium",
    "forecast",
    "picks",
}
LIVE_FIELDS = {
    "quarter",
    "time_remaining",
    "possession",
    "down",
    "distance",
    "down_distance_text",
    "yard_line",
    "possession_text",
    "is_red_zone",
    "home_timeouts",
    "away_timeouts",
    "last_play",
    "drive_text",
    "drive_start",
    "win_probability",
    "weather",
}
WEATHER_FIELDS = {
    "temp_f",
    "feels_like_f",
    "condition",
    "icon",
    "precip_type",
    "wind_speed_mph",
    "wind_gust_mph",
    "precipitation_pct",
    "visibility_mi",
    "weather_alerts",
}
FORECAST_FIELDS = WEATHER_FIELDS | {
    "wind_direction",
    "during_game",
    "source",
    "captured_at",
}
DURING_GAME_FIELDS = {
    "precipitation_pct_max",
    "precip_type",
    "wind_gust_mph_max",
    "temp_f_low",
    "temp_f_high",
    "snow_accumulation_in",
    "hours",
}
STADIUM_FIELDS = {
    "name",
    "city",
    "state",
    "country",
    "latitude",
    "longitude",
    "roof_type",
    "surface_type",
}
PLAY_FIELDS = {
    "quarter",
    "clock",
    "team_id",
    "type",
    "description",
    "player_name",
    "home_score",
    "away_score",
}
PLAYER_LINE_FIELDS = {"name", "sports_io_player_id", "image", "stats"}

ALERT = {
    "title": "Wind Advisory",
    "severity": "Moderate",
    "starts": 1789318800,
    "expires": 1789347600,
    "uri": "https://alerts.example/1",
}


class Board:
    """One week's games with the rows each part of the scoreboard reads"""

    def __init__(self, seed: Seed, week: int = 1) -> None:
        self.seed = seed
        self.d1 = seed.d1
        self.week_number = week
        self.week_id = seed.week(week)
        self.users: dict[str, int] = {}

    def game(self, **values: Any) -> dict[str, Any]:
        home = self.seed.team(values.pop("home", None))
        away = self.seed.team(values.pop("away", None))
        values.setdefault("game_time", KICKOFF)
        values.setdefault("status", "SCHEDULED")
        game_id = self.seed.game(
            self.week_id, home_team_id=home, away_team_id=away, **values
        )
        return {"game_id": game_id, "home": home, "away": away}

    def pick(self, game: dict[str, Any], side: str, *names: str) -> None:
        for name in names:
            if name not in self.users:
                self.users[name] = self.seed.user(name)
            self.seed.pick(self.users[name], game["game_id"], game[side])

    def snapshot(self, game: dict[str, Any], **values: Any) -> int:
        return self.seed._insert("game_snapshots", game_id=game["game_id"], **values)

    def play(
        self, game: dict[str, Any], sequence: int, score: tuple[int, int], **values: Any
    ) -> None:
        self.seed._insert(
            "game_scoring_plays",
            game_id=game["game_id"],
            sequence=sequence,
            home_score=score[0],
            away_score=score[1],
            **values,
        )

    def player(
        self, game: dict[str, Any], side: str, group: str, name: str, **stats: Any
    ) -> None:
        self.seed._insert(
            "game_player_stats",
            game_id=game["game_id"],
            team_id=game[side],
            stat_group=group,
            player_name=name,
            sports_io_player_id=sum(map(ord, name)),
            player_image=f"https://img.example/{name}.png",
            stats_json=json.dumps(stats),
        )

    def team_stats(self, game: dict[str, Any], side: str, **stats: Any) -> None:
        self.seed._insert(
            "game_team_stats", game_id=game["game_id"], team_id=game[side], **stats
        )


@pytest.fixture
def board(seed: Seed) -> Board:
    return Board(seed)


@pytest.fixture(autouse=True)
def _fakes(clients: Clients) -> None:
    """every test here writes through the fake D1/KV"""


def _week_key(clients: Clients, week: int = 1) -> dict[str, Any]:
    return clients.kv.values[f"week:{SEASON}:{week:02d}:games"]


def _games(clients: Clients, week: int = 1) -> dict[int, dict[str, Any]]:
    games.write_week_games(week)
    return {g["game_id"]: g for g in _week_key(clients, week)["games"]}


class TestGamesContract:
    def test_scheduled_game_shape(self, clients: Clients, board: Board) -> None:
        game = board.game(home="KC", away="BUF", cbs_spread=-2.5)
        board.pick(game, "home", "a")

        written = _games(clients)[game["game_id"]]

        assert set(_week_key(clients)) == {"week", "updated_at", "games"}
        assert set(written) == GAME_FIELDS
        assert set(written["home_team"]) == {"id", "abbr", "name", "record"}
        assert set(written["picks"]) == {"home", "away"}
        assert written["picks"]["home"] == [{"user_id": board.users["a"], "name": "a"}]

    def test_live_game_shape(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS")
        board.snapshot(game, quarter=2, temperature_f=61, weather_alerts_json="[]")

        live = _games(clients)[game["game_id"]]["live"]

        assert set(live) == LIVE_FIELDS
        assert set(live["weather"]) == WEATHER_FIELDS
        assert set(live["last_play"]) == {"text", "type"}
        assert set(live["drive_start"]) == {"yard_line", "text"}
        assert set(live["win_probability"]) == {"home", "away"}

    def test_forecast_and_stadium_shape(self, clients: Clients, board: Board) -> None:
        stadium = board.seed._insert("stadiums", name="Arrowhead", city="Kansas City")
        game = board.game(
            stadium_id=stadium, forecast_captured_at="2026-09-12T12:00:00Z"
        )

        written = _games(clients)[game["game_id"]]

        assert set(written["stadium"]) == STADIUM_FIELDS
        assert set(written["forecast"]) == FORECAST_FIELDS
        assert set(written["forecast"]["during_game"]) == DURING_GAME_FIELDS

    def test_scoring_play_and_leader_shape(
        self, clients: Clients, board: Board
    ) -> None:
        game = board.game(status="FINAL", home_score=7, away_score=0)
        board.play(game, 1, (7, 0), quarter=1, clock="8:26", type="TD")
        board.player(game, "home", "Passing", "QB", yards=250)

        written = _games(clients)[game["game_id"]]

        assert set(written["scoring_plays"][0]) == PLAY_FIELDS
        assert set(written["leaders"]) == {"home", "away"}
        assert set(written["leaders"]["home"]) == {"passing", "rushing", "receiving"}
        assert set(written["leaders"]["home"]["passing"]) == PLAYER_LINE_FIELDS


class TestScheduledGame:
    def test_empty_parts_before_kickoff(self, clients: Clients, board: Board) -> None:
        game = board.game()

        written = _games(clients)[game["game_id"]]

        assert written["status"] == "SCHEDULED"
        assert written["linescore"] is None
        assert written["leaders"] is None
        assert written["scoring_plays"] == []
        assert written["forecast"] is None
        assert written["stadium"] is None
        assert written["home_team"]["record"] is None
        assert written["picks"] == {"home": [], "away": []}
        assert written["neutral_site"] is False
        assert "live" not in written

    def test_team_record(self, clients: Clients, board: Board, seed: Seed) -> None:
        game = board.game()
        seed.d1.query(
            "UPDATE teams SET wins = 2, losses = 1, ties = 0 WHERE team_id = ?",
            [game["home"]],
        )

        written = _games(clients)[game["game_id"]]

        assert written["home_team"]["record"] == {"wins": 2, "losses": 1, "ties": 0}
        assert written["away_team"]["record"] is None

    def test_picks_split_by_side(self, clients: Clients, board: Board) -> None:
        game = board.game()
        board.pick(game, "home", "a", "b")
        board.pick(game, "away", "c")

        picks = _games(clients)[game["game_id"]]["picks"]

        assert [p["name"] for p in picks["home"]] == ["a", "b"]
        assert [p["name"] for p in picks["away"]] == ["c"]

    def test_games_in_kickoff_order(self, clients: Clients, board: Board) -> None:
        late = board.game(game_time=KICKOFF + timedelta(hours=3))
        early = board.game(game_time=KICKOFF)
        board.game(game_time=KICKOFF + timedelta(days=1))

        games.write_week_games(1)
        ids = [g["game_id"] for g in _week_key(clients)["games"]]

        assert ids[:2] == [early["game_id"], late["game_id"]]

    def test_neutral_site_and_stadium(self, clients: Clients, board: Board) -> None:
        stadium = board.seed._insert(
            "stadiums",
            name="Tottenham Hotspur Stadium",
            city="London",
            country="England",
            roof_type="Retractable",
        )
        game = board.game(stadium_id=stadium, neutral_site=True)

        written = _games(clients)[game["game_id"]]

        assert written["neutral_site"] is True
        assert written["stadium"]["country"] == "England"
        assert written["stadium"]["roof_type"] == "Retractable"


class TestForecast:
    def test_full_forecast(self, clients: Clients, board: Board) -> None:
        hours = [
            {"time": "2026-09-13T17:00:00Z", "temp_f": 71, "precip_pct": 38},
            {"time": "2026-09-13T18:00:00Z", "temp_f": 69, "precip_pct": 68},
        ]
        game = board.game(
            forecast_temp_f=71,
            forecast_condition="Light Rain",
            forecast_icon="rain",
            forecast_wind_direction="NW",
            forecast_alerts_json=json.dumps([ALERT]),
            forecast_window_precip_pct_max=68,
            forecast_window_temp_f_low=69,
            forecast_hours_json=json.dumps(hours),
            forecast_source="hourly",
            forecast_captured_at="2026-09-13T08:42:00Z",
        )

        forecast = _games(clients)[game["game_id"]]["forecast"]

        assert (forecast["temp_f"], forecast["icon"]) == (71, "rain")
        assert forecast["weather_alerts"] == [ALERT]
        assert forecast["during_game"]["precipitation_pct_max"] == 68
        assert forecast["during_game"]["hours"] == hours
        assert forecast["source"] == "hourly"
        assert forecast["captured_at"] == "2026-09-13T08:42:00Z"

    def test_missing_json_reads_as_empty_lists(
        self, clients: Clients, board: Board
    ) -> None:
        # a daily-source forecast, or one captured before hours existed
        game = board.game(
            forecast_source="daily", forecast_captured_at="2026-09-06T08:00:00Z"
        )

        forecast = _games(clients)[game["game_id"]]["forecast"]

        assert forecast["weather_alerts"] == []
        assert forecast["during_game"]["hours"] == []
        assert forecast["temp_f"] is None


class TestLinescore:
    def test_quarters_not_played_stay_null(
        self, clients: Clients, board: Board
    ) -> None:
        game = board.game(
            status="HALFTIME",
            home_q1_score=7,
            home_q2_score=7,
            away_q1_score=0,
            away_q2_score=3,
        )

        linescore = _games(clients)[game["game_id"]]["linescore"]

        assert linescore == {
            "home": {"q1": 7, "q2": 7, "q3": None, "q4": None, "ot": None},
            "away": {"q1": 0, "q2": 3, "q3": None, "q4": None, "ot": None},
        }

    def test_a_scoreless_quarter_is_not_empty(
        self, clients: Clients, board: Board
    ) -> None:
        game = board.game(status="IN_PROGRESS", home_q1_score=0, away_q1_score=0)
        assert _games(clients)[game["game_id"]]["linescore"] is not None


class TestLiveBlock:
    @pytest.mark.parametrize("status", ["IN_PROGRESS", "HALFTIME", "DELAYED"])
    def test_live_statuses(self, clients: Clients, board: Board, status: str) -> None:
        game = board.game(status=status)
        board.snapshot(game, quarter=2)
        assert "live" in _games(clients)[game["game_id"]]

    @pytest.mark.parametrize("status", ["SCHEDULED", "FINAL", "POSTPONED"])
    def test_no_live_block_otherwise(
        self, clients: Clients, board: Board, status: str
    ) -> None:
        # a FINAL game keeps its old snapshots - they mustn't show as live
        game = board.game(status=status)
        board.snapshot(game, quarter=4)
        assert "live" not in _games(clients)[game["game_id"]]

    def test_no_snapshot_yet(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS")
        assert "live" not in _games(clients)[game["game_id"]]

    def test_uses_the_latest_snapshot(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS")
        board.snapshot(game, quarter=1, time_remaining="12:00")
        board.snapshot(game, quarter=2, time_remaining="3:15", possession="HOME")

        live = _games(clients)[game["game_id"]]["live"]

        assert (live["quarter"], live["time_remaining"]) == (2, "3:15")
        assert live["possession"] == "HOME"

    def test_field_state(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS")
        board.snapshot(
            game,
            quarter=3,
            down=3,
            distance=7,
            down_distance_text="3rd & 7 at SEA 18",
            yard_line=18,
            possession_text="SEA 18",
            is_red_zone=True,
            last_play_text="Pass complete for 9 yards",
            last_play_type="Pass Reception",
            drive_text="6 plays, 55 yards, 3:10",
            drive_start_yard_line=63,
            drive_start_text="LAR 37",
            home_win_pct=71.4,
            away_win_pct=28.6,
        )

        live = _games(clients)[game["game_id"]]["live"]

        assert (live["down"], live["distance"], live["yard_line"]) == (3, 7, 18)
        assert live["is_red_zone"] is True
        assert live["last_play"] == {
            "text": "Pass complete for 9 yards",
            "type": "Pass Reception",
        }
        assert live["drive_start"] == {"yard_line": 63, "text": "LAR 37"}
        assert live["win_probability"] == {"home": 71.4, "away": 28.6}

    def test_no_spot_yard_line_is_null(self, clients: Clients, board: Board) -> None:
        # ESPN's 0 means "no spot" (halftime), never a real ball position
        game = board.game(status="HALFTIME")
        board.snapshot(game, quarter=2, yard_line=0)

        live = _games(clients)[game["game_id"]]["live"]

        assert live["yard_line"] is None
        assert live["is_red_zone"] is False

    def test_weather(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS")
        board.snapshot(
            game,
            quarter=1,
            temperature_f=44,
            weather_condition="Breezy",
            weather_icon="wind",
            wind_gust_mph=34,
            weather_alerts_json=json.dumps([ALERT]),
        )

        weather = _games(clients)[game["game_id"]]["live"]["weather"]

        assert (weather["temp_f"], weather["icon"], weather["wind_gust_mph"]) == (
            44,
            "wind",
            34,
        )
        assert weather["weather_alerts"] == [ALERT]

    def test_enclosed_stadium_has_no_weather(
        self, clients: Clients, board: Board
    ) -> None:
        game = board.game(status="IN_PROGRESS")
        board.snapshot(game, quarter=1, wind_speed_mph=5)  # no temperature
        assert _games(clients)[game["game_id"]]["live"]["weather"] is None


class TestPreferSnapshotScore:
    def test_snapshot_ahead_of_sports_io(
        self, clients: Clients, board: Board, d1: FakeD1
    ) -> None:
        game = board.game(status="IN_PROGRESS", home_score=7, away_score=3)
        board.snapshot(game, quarter=2, home_score=14, away_score=3)

        written = _games(clients)[game["game_id"]]

        assert (written["home_score"], written["away_score"]) == (14, 3)
        # D1 keeps Sports IO's score
        row = d1.query(
            "SELECT home_score FROM games WHERE game_id = ?", [game["game_id"]]
        )
        assert row.results[0]["home_score"] == 7

    def test_older_snapshot_never_wins(self, clients: Clients, board: Board) -> None:
        # ESPN stopped answering - Sports IO has moved on since
        game = board.game(status="IN_PROGRESS", home_score=14, away_score=10)
        board.snapshot(game, quarter=2, home_score=14, away_score=3)

        written = _games(clients)[game["game_id"]]

        assert (written["home_score"], written["away_score"]) == (14, 10)

    def test_snapshot_without_a_score(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS", home_score=3, away_score=0)
        board.snapshot(game, quarter=1, home_score=None, away_score=None)

        written = _games(clients)[game["game_id"]]

        assert (written["home_score"], written["away_score"]) == (3, 0)

    def test_before_sports_io_has_a_score(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS")
        board.snapshot(game, quarter=1, home_score=0, away_score=7)

        written = _games(clients)[game["game_id"]]

        assert (written["home_score"], written["away_score"]) == (0, 7)


class TestScoringPlays:
    def test_in_sequence_per_game(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS")
        other = board.game(status="IN_PROGRESS")
        board.play(
            game, 2, (7, 3), quarter=2, clock=None, type="TD", team_id=game["home"]
        )
        board.play(
            game, 1, (0, 3), quarter=1, clock="8:26", type="FG", team_id=game["away"]
        )
        board.play(other, 1, (0, 2), quarter=3, type="SF")

        written = _games(clients)

        plays = written[game["game_id"]]["scoring_plays"]
        assert [(p["type"], p["home_score"], p["away_score"]) for p in plays] == [
            ("FG", 0, 3),
            ("TD", 7, 3),
        ]
        assert plays[1]["clock"] is None  # Sports IO leaves it out on ~15%
        assert [p["type"] for p in written[other["game_id"]]["scoring_plays"]] == ["SF"]


class TestLeaders:
    def test_top_player_per_category(self, clients: Clients, board: Board) -> None:
        game = board.game(status="FINAL")
        board.player(game, "home", "Passing", "Starter", yards=292, comp_att="19/34")
        board.player(game, "home", "Passing", "Backup", yards=12)
        board.player(game, "home", "Rushing", "RB1", yards=40)
        board.player(game, "home", "Rushing", "RB2", yards=88)
        board.player(game, "away", "Receiving", "WR", yards=101)
        board.player(game, "away", "Defensive", "LB", tackles=12)  # not a leader

        leaders = _games(clients)[game["game_id"]]["leaders"]

        assert leaders["home"]["passing"]["name"] == "Starter"
        assert leaders["home"]["passing"]["stats"]["comp_att"] == "19/34"
        assert leaders["home"]["rushing"]["name"] == "RB2"
        assert leaders["home"]["receiving"] is None
        assert leaders["away"]["receiving"]["name"] == "WR"
        assert leaders["away"]["passing"] is None

    def test_lines_without_numeric_yards_are_skipped(
        self, clients: Clients, board: Board
    ) -> None:
        game = board.game(status="IN_PROGRESS")
        board.player(game, "home", "Rushing", "Unknown", yards=None)
        board.player(game, "home", "Rushing", "Known", yards=-2)

        leaders = _games(clients)[game["game_id"]]["leaders"]

        assert leaders["home"]["rushing"]["name"] == "Known"


class TestWeekScoping:
    def test_other_weeks_stay_out(
        self, clients: Clients, board: Board, seed: Seed
    ) -> None:
        game = board.game(status="IN_PROGRESS")
        board.pick(game, "home", "a")
        board.snapshot(game, quarter=1)
        week2 = Board(seed, week=2)
        week2.users = board.users
        other = week2.game(status="IN_PROGRESS")
        week2.pick(other, "away", "a")
        week2.snapshot(other, quarter=4)

        week1 = _games(clients, 1)
        written2 = _games(clients, 2)

        assert set(week1) == {game["game_id"]}
        assert week1[game["game_id"]]["live"]["quarter"] == 1
        assert written2[other["game_id"]]["picks"]["home"] == []
        assert written2[other["game_id"]]["live"]["quarter"] == 4

    def test_no_games_writes_nothing(self, clients: Clients, seed: Seed) -> None:
        seed.week(1)
        games.write_week_games(1)
        assert clients.kv.values == {}


def _week(seed: Seed, number: int, start: timedelta, **flags: bool) -> None:
    now = datetime.now(UTC)
    week_id = seed.week(number)
    seed.d1.query(
        "UPDATE weeks SET start_time = ?, is_complete = ?, is_current = ? WHERE week_id = ?",
        [
            iso(now + start),
            flags.get("complete", False),
            flags.get("current", False),
            week_id,
        ],
    )
    seed.game(week_id, game_time=now + start, status="SCHEDULED")


class TestWhichWeeksGetWritten:
    def test_current_week(self, clients: Clients, seed: Seed) -> None:
        _week(seed, 3, timedelta(days=2), current=True)
        _week(seed, 4, timedelta(days=9))

        games.write_current_week_games()

        assert set(clients.kv.values) == {f"week:{SEASON}:03:games"}

    def test_no_current_week(self, clients: Clients, seed: Seed) -> None:
        _week(seed, 1, timedelta(days=2))
        games.write_current_week_games()
        assert clients.kv.values == {}

    def _season(self, seed: Seed) -> None:
        day = timedelta(days=1)
        _week(seed, 1, -10 * day, complete=True)
        _week(seed, 2, -3 * day)  # MNF still going after CBS moved on
        _week(seed, 3, 2 * day, current=True)
        _week(seed, 4, 9 * day)
        _week(seed, 5, 16 * day)

    def test_incomplete_weeks_that_have_started(
        self, clients: Clients, seed: Seed
    ) -> None:
        self._season(seed)
        games.write_incomplete_weeks_games()
        assert sorted(clients.kv.values) == [
            f"week:{SEASON}:02:games",
            f"week:{SEASON}:03:games",
        ]

    def test_daily_refresh_includes_future_weeks(
        self, clients: Clients, seed: Seed
    ) -> None:
        self._season(seed)
        games.write_incomplete_weeks_games(include_future=True)
        assert sorted(clients.kv.values) == [
            f"week:{SEASON}:0{n}:games" for n in (2, 3, 4, 5)
        ]

    def test_live_ticker_rewrites_only_the_changed_weeks(
        self, clients: Clients, board: Board, seed: Seed
    ) -> None:
        game = board.game(status="IN_PROGRESS")
        Board(seed, week=2).game()
        other = Board(seed, week=3).game(status="IN_PROGRESS")

        games.write_games_weeks({game["game_id"], other["game_id"]})

        assert sorted(clients.kv.values) == [
            f"week:{SEASON}:01:games",
            f"week:{SEASON}:03:games",
        ]

    def test_live_ticker_with_nothing_changed(self, clients: Clients) -> None:
        games.write_games_weeks(set())
        assert clients.kv.values == {}


def _details(clients: Clients, game: dict[str, Any]) -> dict[str, Any]:
    game_details.write_game_details([game["game_id"]])
    return clients.kv.values[f"game:{SEASON}:{game['game_id']}:details"]


class TestGameDetails:
    def test_contract(self, clients: Clients, board: Board, d1: FakeD1) -> None:
        game = board.game(status="FINAL")
        board.team_stats(game, "home", yards_total=380, passing_yards=250)
        board.team_stats(game, "away", yards_total=290)
        board.player(game, "home", "Passing", "QB", yards=250)
        d1.query(
            "INSERT INTO game_win_probability (game_id, points_json) VALUES (?, ?)",
            [game["game_id"], json.dumps([{"period": 0, "home_win_pct": 55.0}])],
        )

        details = _details(clients, game)

        assert set(details) == {
            "game_id",
            "updated_at",
            "box_score",
            "players",
            "win_probability",
            "win_probability_source",
        }
        home_box = details["box_score"]["home"]
        assert {"stat_id", "game_id", "team_id"}.isdisjoint(home_box)
        assert {
            "yards_total",
            "passing_yards",
            "punts",
            "punt_yards",
            "punt_average",
        } <= set(home_box)
        assert set(details["players"]["home"]["passing"][0]) == PLAYER_LINE_FIELDS
        assert details["win_probability"] == [{"period": 0, "home_win_pct": 55.0}]
        assert details["win_probability_source"] == "final"

    def test_game_with_no_data_is_skipped(self, clients: Clients, board: Board) -> None:
        game = board.game()
        game_details.write_game_details([game["game_id"]])
        assert clients.kv.values == {}

    def test_nothing_to_write(self, clients: Clients) -> None:
        game_details.write_game_details([])
        assert clients.kv.values == {}

    def test_live_win_probability_from_snapshots(
        self, clients: Clients, board: Board
    ) -> None:
        game = board.game(status="IN_PROGRESS")
        snap = {"quarter": 1, "time_remaining": "15:00", "home_score": 0}
        board.snapshot(game, **snap, home_win_pct=55.0, away_score=0)
        # a timeout: snapshot changed, the point didn't
        board.snapshot(game, **snap, home_win_pct=55.0, away_score=0)
        # no win probability (e.g. halftime) - skipped
        board.snapshot(game, **snap, home_win_pct=None, away_score=0)
        board.snapshot(
            game,
            quarter=1,
            time_remaining="9:12",
            home_win_pct=41.3,
            home_score=0,
            away_score=7,
        )
        board.snapshot(
            game,
            quarter=1,
            time_remaining="8:40",
            home_win_pct=39.0,
            home_score=0,
            away_score=7,
        )

        details = _details(clients, game)

        assert details["win_probability_source"] == "live"
        assert details["win_probability"] == [
            {
                "period": 1,
                "clock": "15:00",
                "home_win_pct": 55.0,
                "home_score": 0,
                "away_score": 0,
                "scoring_play": False,
            },
            {
                "period": 1,
                "clock": "9:12",
                "home_win_pct": 41.3,
                "home_score": 0,
                "away_score": 7,
                "scoring_play": True,
            },
            {
                "period": 1,
                "clock": "8:40",
                "home_win_pct": 39.0,
                "home_score": 0,
                "away_score": 7,
                "scoring_play": False,
            },
        ]

    def test_final_curve_replaces_snapshots(
        self, clients: Clients, board: Board, d1: FakeD1
    ) -> None:
        game = board.game(status="FINAL")
        board.snapshot(game, quarter=4, home_win_pct=99.0, home_score=7, away_score=0)
        curve = [{"period": 0, "home_win_pct": 55.0}]
        d1.query(
            "INSERT INTO game_win_probability (game_id, points_json) VALUES (?, ?)",
            [game["game_id"], json.dumps(curve)],
        )

        details = _details(clients, game)

        assert details["win_probability"] == curve
        assert details["win_probability_source"] == "final"

    def test_no_win_probability_yet(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS")
        board.player(game, "home", "Passing", "QB", yards=40)
        board.snapshot(game, quarter=1, home_win_pct=None)

        details = _details(clients, game)

        assert details["win_probability"] is None
        assert details["win_probability_source"] is None

    def test_box_score_needs_both_teams(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS")
        board.team_stats(game, "home", yards_total=100)
        board.player(game, "home", "Rushing", "RB", yards=40)

        details = _details(clients, game)

        assert details["box_score"] is None
        assert details["players"] is not None

    def test_punting_from_player_lines(self, clients: Clients, board: Board) -> None:
        game = board.game(status="FINAL")
        board.team_stats(game, "home")
        board.team_stats(game, "away")
        # two punters, 201 yards on 4 punts: 50.25 rounds half-up to 50.3
        board.player(game, "home", "Punting", "Punter", total=3, yards=150)
        board.player(game, "home", "Punting", "Kicker", total=1, yards=51)
        board.player(game, "away", "Passing", "QB", yards=200)

        box = _details(clients, game)["box_score"]

        assert (box["home"]["punts"], box["home"]["punt_yards"]) == (4, 201)
        assert box["home"]["punt_average"] == 50.3
        # player stats exist, the away team just never punted
        assert (box["away"]["punts"], box["away"]["punt_average"]) == (0, None)

    def test_punting_before_player_stats(self, clients: Clients, board: Board) -> None:
        game = board.game(status="IN_PROGRESS")
        board.team_stats(game, "home")
        board.team_stats(game, "away")

        box = _details(clients, game)["box_score"]

        assert box["home"]["punts"] is None
        assert box["home"]["punt_average"] is None

    def test_players_grouped_and_ranked(self, clients: Clients, board: Board) -> None:
        game = board.game(status="FINAL")
        board.player(game, "home", "Defensive", "Backup", tackles=3)
        board.player(game, "home", "Defensive", "Star", tackles=12)
        board.player(game, "home", "Defensive", "Unknown", tackles=None)
        board.player(game, "home", "Kick_returns", "KR", yards=45)
        # a negative line still ranks above one with no number at all
        board.player(game, "home", "Rushing", "Kneel", yards=-3)
        board.player(game, "home", "Rushing", "NoStat", yards=None)
        board.player(game, "away", "Kicking", "K1", points=4)
        board.player(game, "away", "Kicking", "K2", points=9)

        players = _details(clients, game)["players"]

        assert [p["name"] for p in players["home"]["defensive"]] == [
            "Star",
            "Backup",
            "Unknown",  # no number sorts last
        ]
        assert "kick_returns" in players["home"]
        assert [p["name"] for p in players["home"]["rushing"]] == ["Kneel", "NoStat"]
        assert [p["name"] for p in players["away"]["kicking"]] == ["K2", "K1"]

    def test_week_refresh(self, clients: Clients, board: Board, seed: Seed) -> None:
        games_with_stats = [board.game(status="FINAL") for _ in range(2)]
        for game in games_with_stats:
            board.player(game, "home", "Passing", "QB", yards=100)
        other_week = Board(seed, week=2).game(status="FINAL")
        board.player(other_week, "home", "Passing", "QB", yards=100)

        game_details.write_week_game_details(1)

        assert sorted(clients.kv.values) == sorted(
            f"game:{SEASON}:{g['game_id']}:details" for g in games_with_stats
        )

    def test_current_week_refresh(
        self, clients: Clients, board: Board, d1: FakeD1
    ) -> None:
        game = board.game(status="FINAL")
        board.player(game, "home", "Passing", "QB", yards=100)
        game_details.write_current_week_game_details()
        assert clients.kv.values == {}  # no current week yet

        d1.query("UPDATE weeks SET is_current = 1")
        game_details.write_current_week_game_details()
        assert set(clients.kv.values) == {f"game:{SEASON}:{game['game_id']}:details"}
