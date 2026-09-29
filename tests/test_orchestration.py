"""src/orchestration.py's minute tick.

Wiring tests swap every loader/KV writer for a recorder, to pin what runs
when and that a failing task never ends the tick. The end-to-end tests run
real ticks - every loader and KV writer - against the saved API responses,
fake D1 and fake KV."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from config.config import SEASON, CBSConfig
from src import orchestration
from src.kv_writer import (
    admin,
    game_details,
    games,
    historical,
    leaderboard,
    odds,
    recap,
    shared,
    trends,
    user_profiles,
)
from src.loaders import cbs_loader, stadiums_loader, teams_loader
from src.scheduling import get_state, set_state
from tests.api_fixtures import FakeApis, FakeCBS, capture_info
from tests.conftest import Clients, FakeD1, Seed, freeze_all, iso

# every loader/KV writer orchestration calls
TASKS = (
    "load_games_data",
    "load_teams",
    "load_cbs_weeks",
    "load_cbs_games",
    "load_cbs_user_picks",
    "load_espn_games",
    "load_the_odds_api_odds",
    "load_pregame_weather",
    "load_scoring_plays",
    "load_game_statistics",
    "load_live_game_statistics",
    "load_week_player_stats",
    "load_live_player_stats",
    "load_final_win_probability",
    "write_meta_current",
    "write_incomplete_weeks_games",
    "write_current_week_games",
    "write_current_week_leaderboard",
    "write_current_week_odds",
    "write_current_week_trends",
    "write_season_trends",
    "write_recent_weeks_recap",
    "write_user_profiles",
    "write_game_details",
    "write_admin_status",
)

# a quiet Wednesday: nothing live, the Sunday deadline still ahead
QUIET = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)


class Recorder:
    """Stands in for every task: records calls in order, returns an empty
    set (what the loaders feeding game details return), or raises what
    `fail` says to."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.args: dict[str, list[tuple[Any, ...]]] = {}
        self.fail: dict[str, Exception] = {}
        for name in TASKS:
            monkeypatch.setattr(orchestration, name, self._task(name))

    def _task(self, name: str) -> Callable[..., set[int]]:
        def task(*args: Any, **kwargs: Any) -> set[int]:
            self.calls.append(name)
            self.args.setdefault(name, []).append((*args, *kwargs.values()))
            if name in self.fail:
                raise self.fail[name]
            return set()

        return task

    def count(self, name: str) -> int:
        return self.calls.count(name)


@pytest.fixture
def tasks(monkeypatch: pytest.MonkeyPatch, clients: Clients) -> Recorder:
    clients.use(orchestration)
    freeze_all(monkeypatch, QUIET)
    return Recorder(monkeypatch)


def _events(d1: FakeD1) -> list[str]:
    return [r["source"] for r in d1.query("SELECT source FROM system_events").results]


def _game_at(seed: Seed, kickoff: datetime, status: str = "SCHEDULED") -> int:
    week_id = seed.d1.query("SELECT week_id FROM weeks LIMIT 1").results
    return seed.game(
        week_id[0]["week_id"] if week_id else seed.week(1),
        game_time=kickoff,
        status=status,
    )


class TestTickWiring:
    def test_quiet_tick(self, tasks: Recorder, d1: FakeD1) -> None:
        orchestration.main()

        for task in (
            "load_the_odds_api_odds",
            "load_cbs_user_picks",
            "load_games_data",  # housekeeping
            "write_incomplete_weeks_games",
            "write_admin_status",
        ):
            assert tasks.count(task) >= 1, task
        assert "load_live_game_statistics" not in tasks.calls
        assert tasks.calls[-1] == "write_admin_status"
        assert _events(d1) == []

    def test_live_tick(self, tasks: Recorder, seed: Seed, d1: FakeD1) -> None:
        _game_at(seed, QUIET - timedelta(hours=1), status="IN_PROGRESS")

        orchestration.main()

        assert tasks.args["load_games_data"] == [(True,)]  # live poll only
        for task in (
            "load_cbs_user_picks",
            "load_live_game_statistics",
            "load_live_player_stats",
        ):
            assert tasks.count(task) == 1, task
        assert "load_the_odds_api_odds" not in tasks.calls  # quiet-only
        assert "load_teams" not in tasks.calls  # housekeeping is quiet-only

    def test_games_key_is_written_before_weeks_are_marked_complete(
        self, tasks: Recorder, seed: Seed
    ) -> None:
        # the tick a week's last game goes FINAL must still rewrite its key
        _game_at(seed, QUIET - timedelta(days=1), status="FINAL")

        orchestration.main()

        assert tasks.calls.index("write_incomplete_weeks_games") < tasks.calls.index(
            "load_game_statistics"
        )

    @pytest.mark.parametrize(
        "failing",
        [
            "write_incomplete_weeks_games",
            "write_current_week_leaderboard",
            "write_season_trends",
            "load_scoring_plays",
            "load_final_win_probability",
            "load_the_odds_api_odds",
        ],
    )
    def test_a_failing_task_never_ends_the_tick(
        self, tasks: Recorder, d1: FakeD1, failing: str
    ) -> None:
        tasks.fail[failing] = RuntimeError(f"{failing} broke")

        orchestration.main()

        # everything after it still ran, meta:admin last
        assert tasks.calls[-1] == "write_admin_status"
        messages = {
            e["message"] for e in d1.query("SELECT message FROM system_events").results
        }
        assert f"{failing} broke" in messages

    def test_second_tick_skips_what_isnt_due(
        self, tasks: Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orchestration.main()
        first = list(tasks.calls)
        tasks.calls.clear()
        freeze_all(monkeypatch, QUIET + timedelta(minutes=1))

        orchestration.main()

        for task in (
            "load_the_odds_api_odds",
            "load_cbs_user_picks",
            "load_games_data",
            "write_user_profiles",
        ):
            assert task in first and task not in tasks.calls, task
        # the every-tick writes still happen
        for task in (
            "write_incomplete_weeks_games",
            "write_current_week_leaderboard",
            "write_admin_status",
        ):
            assert task in tasks.calls, task


class TestHousekeeping:
    def test_a_failing_step_doesnt_skip_the_rest(
        self, tasks: Recorder, d1: FakeD1
    ) -> None:
        tasks.fail["load_cbs_weeks"] = RuntimeError("CBS login failed")

        orchestration.main()

        for later in ("load_cbs_games", "load_espn_games", "write_meta_current"):
            assert later in tasks.calls, later
        # but housekeeping as a whole isn't a success
        assert get_state(d1, "housekeeping_last_run_at") is not None
        assert get_state(d1, "housekeeping_last_success_at") is None
        assert _events(d1) == ["housekeeping"]

    def test_success(self, tasks: Recorder, d1: FakeD1) -> None:
        orchestration.main()
        assert get_state(d1, "housekeeping_last_success_at") is not None
        # the daily refresh also covers future weeks' games keys
        assert (True,) in tasks.args["write_incomplete_weeks_games"]


class TestDeadlineSweep:
    # Sunday 2026-10-04, 1:05pm ET
    AFTER = datetime(2026, 10, 4, 17, 5, tzinfo=UTC)

    def _sweeps(self, tasks: Recorder) -> int:
        return tasks.count("write_current_week_games")

    def test_runs_once_after_the_deadline(
        self, tasks: Recorder, d1: FakeD1, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orchestration.main()
        assert self._sweeps(tasks) == 0  # Wednesday - not yet

        freeze_all(monkeypatch, self.AFTER)
        orchestration.main()
        orchestration.main()

        assert self._sweeps(tasks) == 1
        assert get_state(d1, "deadline_last_synced_sunday") == "2026-10-04"

    def test_failure_retries_after_ten_minutes(
        self, tasks: Recorder, d1: FakeD1, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        freeze_all(monkeypatch, self.AFTER)
        tasks.fail["load_cbs_user_picks"] = RuntimeError("CBS down")
        orchestration.main()
        assert get_state(d1, "deadline_last_synced_sunday") is None

        del tasks.fail["load_cbs_user_picks"]
        freeze_all(monkeypatch, self.AFTER + timedelta(minutes=5))
        orchestration.main()
        assert self._sweeps(tasks) == 0  # waiting out the retry gap

        freeze_all(monkeypatch, self.AFTER + timedelta(minutes=11))
        orchestration.main()
        assert self._sweeps(tasks) == 1
        assert get_state(d1, "deadline_last_synced_sunday") == "2026-10-04"


class TestFinishedGameStats:
    def _week(self, seed: Seed, statuses: list[str]) -> int:
        week_id = seed.week(3)
        for i, status in enumerate(statuses):
            seed.game(
                week_id, game_time=QUIET - timedelta(days=3, hours=i), status=status
            )
        return week_id

    def _flags(self, d1: FakeD1, week_id: int) -> tuple[list[int], int]:
        games_rows = d1.query(
            "SELECT has_final_stats FROM games WHERE week_id = ? ORDER BY game_id",
            [week_id],
        ).results
        week = d1.query(
            "SELECT is_complete FROM weeks WHERE week_id = ?", [week_id]
        ).results[0]
        return [g["has_final_stats"] for g in games_rows], week["is_complete"]

    def test_marks_final_games_and_the_complete_week(
        self, tasks: Recorder, seed: Seed, d1: FakeD1
    ) -> None:
        week_id = self._week(seed, ["FINAL", "FINAL"])

        orchestration.main()

        assert tasks.args["load_game_statistics"] == [(3,)]
        assert self._flags(d1, week_id) == ([1, 1], 1)
        orchestration.main()  # nothing left to finish
        assert tasks.count("load_game_statistics") == 1

    def test_week_with_a_game_left_isnt_complete(
        self, tasks: Recorder, seed: Seed, d1: FakeD1
    ) -> None:
        week_id = self._week(seed, ["FINAL", "SCHEDULED"])
        orchestration.main()
        assert self._flags(d1, week_id) == ([1, 0], 0)

    def test_failed_team_stats_retry_later(
        self, tasks: Recorder, seed: Seed, d1: FakeD1, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        week_id = self._week(seed, ["FINAL"])
        tasks.fail["load_game_statistics"] = RuntimeError("Sports IO down")

        orchestration.main()
        assert self._flags(d1, week_id) == ([0], 0)

        del tasks.fail["load_game_statistics"]
        freeze_all(monkeypatch, QUIET + timedelta(minutes=1))
        orchestration.main()
        assert tasks.count("load_game_statistics") == 1  # waiting out the gap

        freeze_all(monkeypatch, QUIET + timedelta(minutes=11))
        orchestration.main()
        assert self._flags(d1, week_id) == ([1], 1)

    def test_failed_player_stats_still_finish_the_week(
        self, tasks: Recorder, seed: Seed, d1: FakeD1
    ) -> None:
        week_id = self._week(seed, ["FINAL"])
        tasks.fail["load_week_player_stats"] = RuntimeError("Sports IO hiccup")

        orchestration.main()

        assert self._flags(d1, week_id) == ([1], 1)
        assert "final_player_stats" in _events(d1)


class TestEndToEnd:
    """Real ticks: every loader and KV writer against the saved responses.
    The database starts as new_season.py leaves it (season, stadiums,
    teams, CBS team ids and users); the first tick's housekeeping loads
    everything else."""

    @pytest.fixture
    def tick(
        self,
        apis: FakeApis,
        loaders: Clients,
        seed: Seed,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Callable[[datetime], FakeD1]:
        cbs = FakeCBS(monkeypatch, capture_info()["week"])
        # the saved page is from Sunday night - by Wednesday CBS shows
        # every game final (its status overwrites Sports IO's on load)
        for event in cbs.home["poolPeriod"]["poolEvents"]:
            event.update(gameStatusDesc="FINAL", gameStatus="F")
        saved_scoreboard = apis.responses["scoreboard"]
        apis.responses["scoreboard"] = lambda week: (
            saved_scoreboard
            if week in (None, capture_info()["week"])
            else {"events": []}
        )
        loaders.use(
            orchestration, admin, game_details, games, historical, leaderboard,
            odds, recap, shared, trends, user_profiles,
        )  # fmt: skip
        monkeypatch.setattr(
            shared, "get_cbs_config", lambda: CBSConfig("u", "p", "pool1")
        )
        seed.season()
        stadiums_loader.load_stadiums()
        teams_loader.main()
        cbs_loader.map_cbs_to_sports_io()
        cbs_loader.load_cbs_users()
        self.kv = loaders.kv
        self.cbs = cbs

        def run(now: datetime) -> FakeD1:
            freeze_all(monkeypatch, now)
            orchestration.main()
            return loaders.d1

        return run

    def test_first_tick_fills_the_database_and_kv(
        self, tick: Callable[[datetime], FakeD1]
    ) -> None:
        d1 = tick(QUIET)

        assert d1.query("SELECT * FROM system_events").results == []
        assert d1.query("SELECT * FROM mapping_gaps").results == []
        week = capture_info()["week"]
        keys = set(self.kv.values)
        for key in (
            "meta:current",
            "meta:admin",
            f"season:{SEASON}:trends",
            *(f"week:{SEASON}:{n:02d}:games" for n in (week, week + 1, week + 2)),
            f"week:{SEASON}:{week:02d}:trends",
            f"week:{SEASON}:{week:02d}:recap",
        ):
            assert key in keys, key
        assert any(k.startswith("game:") and k.endswith(":details") for k in keys)
        assert any(k.startswith("user:") for k in keys)

        # the finished week's final stats landed and it's marked complete
        (week_row,) = d1.query(
            "SELECT is_complete, is_current FROM weeks WHERE week_number = ?", [week]
        ).results
        assert week_row == {"is_complete": 1, "is_current": 1}
        assert (
            d1.query(
                "SELECT COUNT(*) AS n FROM games WHERE status = 'FINAL' AND has_final_stats = 0"
            ).results[0]["n"]
            == 0
        )
        assert (
            d1.query("SELECT COUNT(*) AS n FROM game_win_probability").results[0]["n"]
            > 0
        )

        status = self.kv.values["meta:admin"]
        assert status["system_events"]["distinct_count"] == 0
        assert status["last_run"]["housekeeping"]["stale"] is False
        assert self.kv.values["meta:current"]["current_week"] == week

    def test_second_tick_adds_the_leaderboard(
        self, tick: Callable[[datetime], FakeD1], apis: FakeApis
    ) -> None:
        # the first tick's quiet picks poll ran before housekeeping had
        # loaded the weeks, so the picks land on the next due poll
        tick(QUIET)
        games_calls = sum(1 for endpoint, _ in apis.calls if endpoint.path == "/games")
        week = capture_info()["week"]
        assert f"week:{SEASON}:{week:02d}:leaderboard" not in self.kv.values

        d1 = tick(QUIET + timedelta(minutes=31))

        board = self.kv.values[f"week:{SEASON}:{week:02d}:leaderboard"]
        entries = self.cbs.weekly["standings"]["weekly"]["rankedEntries"]
        assert set(board) == {"week", "second_half_start_week", "paid_places", "users"}
        assert len(board["users"]) == len(entries)
        assert [u["place"] for u in board["users"]] == sorted(
            u["place"] for u in board["users"]
        )
        assert d1.query("SELECT COUNT(*) AS n FROM weekly_performance").results[0][
            "n"
        ] == len(entries)
        # housekeeping is daily - not again
        assert (
            sum(1 for endpoint, _ in apis.calls if endpoint.path == "/games")
            == games_calls
        )
        assert d1.query("SELECT * FROM system_events").results == []

    def test_ticks_survive_every_api_being_down(
        self,
        tick: Callable[[datetime], FakeD1],
        apis: FakeApis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def down(*_: Any) -> Any:
            raise RuntimeError("down")

        for key in list(apis.responses):
            apis.responses[key] = down
        monkeypatch.setattr(cbs_loader, "get_cbs_weekly", down)

        d1 = tick(QUIET)

        # everything failed, the tick finished, and meta:admin says so
        assert "meta:admin" in self.kv.values
        sources = {
            r["source"] for r in d1.query("SELECT source FROM system_events").results
        }
        assert {"odds_capture", "housekeeping", "cbs_picks_quiet_poll"} <= sources
        admin_status = self.kv.values["meta:admin"]
        assert admin_status["system_events"]["distinct_count"] == len(sources)
        assert admin_status["last_run"]["housekeeping"]["stale"] is True


def test_admin_status_stale_flags(
    clients: Clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients.use(admin)
    freeze_all(monkeypatch, QUIET)
    d1 = clients.d1
    set_state(d1, "housekeeping_last_success_at", iso(QUIET - timedelta(hours=47)))
    set_state(d1, "odds_last_success_at", iso(QUIET - timedelta(hours=20)))
    # either odds cursor being fresh is enough
    set_state(d1, "odds_prekickoff_last_success_at", iso(QUIET - timedelta(hours=1)))
    set_state(d1, "recap_last_success_at", iso(QUIET - timedelta(hours=1)))
    set_state(d1, "deadline_last_synced_sunday", "2026-09-27")  # a bare date

    admin.write_admin_status()

    last_run = clients.kv.values["meta:admin"]["last_run"]
    assert last_run["housekeeping"]["stale"] is False
    assert last_run["odds"]["stale"] is False
    assert last_run["recap_write"]["stale"] is True
    assert last_run["user_profiles_write"]["stale"] is True  # never ran
    assert last_run["deadline_last_synced_sunday"] == "2026-09-27"
    # live-only pollers report timestamps, no stale flag
    assert "stale" not in last_run["sports_io_live_poll"]
