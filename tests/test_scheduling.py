"""Orchestration timing: the Sunday pick deadline, interval cursors and the
soft-fail wrapper (src/scheduling.py), the process lock, and the checks that
decide whether live/pre-kickoff/pregame-weather work runs this tick."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from src import orchestration, scheduling
from src.orchestration import EASTERN, _current_week_deadline_utc
from tests.conftest import FakeD1, Seed


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(seconds: float) -> str:
    return _iso(datetime.now(UTC) - timedelta(seconds=seconds))


def _et(*args: int) -> datetime:
    return datetime(*args, tzinfo=EASTERN)  # type: ignore[misc]


class TestCurrentWeekDeadline:
    # pick weeks run Tuesday through Monday, deadline Sunday 1pm ET
    SUNDAY_EDT = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        "now_et",
        [
            _et(2026, 9, 29, 0, 0),  # Tuesday, just after midnight
            _et(2026, 10, 1, 20, 15),  # Thursday night
            _et(2026, 10, 4, 12, 59),  # a minute before the deadline
            _et(2026, 10, 4, 13, 0),  # the deadline itself
            _et(2026, 10, 4, 20, 20),  # Sunday night, past the deadline
            _et(2026, 10, 5, 23, 59),  # Monday night football
        ],
    )
    def test_whole_week_resolves_to_its_sunday(self, now_et: datetime) -> None:
        assert _current_week_deadline_utc(now_et.astimezone(UTC)) == self.SUNDAY_EDT

    def test_monday_night_is_already_tuesday_in_utc(self) -> None:
        # MNF late: Tuesday 03:30 UTC is still Monday 23:30 ET
        now = datetime(2026, 10, 6, 3, 30, tzinfo=UTC)
        assert _current_week_deadline_utc(now) == self.SUNDAY_EDT

    def test_tuesday_starts_the_next_week(self) -> None:
        now = _et(2026, 10, 6, 0, 0).astimezone(UTC)
        assert _current_week_deadline_utc(now) == self.SUNDAY_EDT + timedelta(days=7)

    def test_dst_ending_sunday_is_1pm_est(self) -> None:
        # DST ends 2am Nov 1 2026 - the deadline that day is 18:00 UTC, not 17:00
        for now_et in (_et(2026, 10, 27, 9, 0), _et(2026, 11, 1, 1, 30)):
            assert _current_week_deadline_utc(now_et.astimezone(UTC)) == datetime(
                2026, 11, 1, 18, 0, tzinfo=UTC
            )

    def test_first_full_est_week(self) -> None:
        now = _et(2026, 11, 3, 12, 0).astimezone(UTC)
        assert _current_week_deadline_utc(now) == datetime(
            2026, 11, 8, 18, 0, tzinfo=UTC
        )


class TestShouldRun:
    def test_never_run_before(self, d1: FakeD1) -> None:
        assert scheduling.should_run(d1, "task", 60)

    def test_not_due_yet(self, d1: FakeD1) -> None:
        scheduling.set_state(d1, "task", _ago(10))
        assert not scheduling.should_run(d1, "task", 60 * 60)

    def test_due(self, d1: FakeD1) -> None:
        scheduling.set_state(d1, "task", _ago(61 * 60))
        assert scheduling.should_run(d1, "task", 60 * 60)

    def test_slack_lets_a_just_short_tick_run(self, d1: FakeD1) -> None:
        # the cursor is stamped when a task finishes, so the next cron tick
        # lands a few seconds short - without slack a 60s task runs every 2 min
        scheduling.set_state(d1, "task", _ago(55))
        assert scheduling.should_run(d1, "task", 60)

    def test_slack_is_bounded(self, d1: FakeD1) -> None:
        scheduling.set_state(d1, "task", _ago(20))
        assert not scheduling.should_run(d1, "task", 60)

    def test_set_state_overwrites(self, d1: FakeD1) -> None:
        scheduling.set_state(d1, "task", "a")
        scheduling.set_state(d1, "task", "b")
        assert scheduling.get_state(d1, "task") == "b"


def _fail() -> None:
    raise RuntimeError("source is down")


class TestSoft:
    def test_success(self, d1: FakeD1) -> None:
        assert scheduling.soft(d1, "src", lambda: None)
        assert d1.query("SELECT * FROM system_events").results == []

    def test_failure_is_recorded_not_raised(self, d1: FakeD1) -> None:
        assert not scheduling.soft(d1, "src", _fail)
        assert not scheduling.soft(d1, "src", _fail)

        events = d1.query("SELECT source, message, occurrences FROM system_events")
        assert events.results == [
            {"source": "src", "message": "source is down", "occurrences": 2}
        ]

    def test_long_messages_are_truncated(self, d1: FakeD1) -> None:
        def fail_long() -> None:
            raise RuntimeError("x" * 1000)

        scheduling.soft(d1, "src", fail_long)
        message = d1.query("SELECT message FROM system_events").results[0]["message"]
        assert len(message) == 500


class TestRunOnInterval:
    @staticmethod
    def _run(d1: FakeD1, task: Callable[[], object]) -> None:
        scheduling.run_on_interval(d1, "src", task, "cursor", "success", 60 * 60)

    def test_success_moves_both_keys(self, d1: FakeD1) -> None:
        calls: list[int] = []
        self._run(d1, lambda: calls.append(1))

        assert calls == [1]
        assert scheduling.get_state(d1, "cursor") is not None
        assert scheduling.get_state(d1, "success") is not None

    def test_failure_moves_only_the_cursor(self, d1: FakeD1) -> None:
        # meta:admin tells a failing task from a healthy one by success_key
        self._run(d1, _fail)

        assert scheduling.get_state(d1, "cursor") is not None
        assert scheduling.get_state(d1, "success") is None

    def test_not_due_skips_the_task(self, d1: FakeD1) -> None:
        scheduling.set_state(d1, "cursor", _ago(60))
        calls: list[int] = []
        self._run(d1, lambda: calls.append(1))

        assert calls == []

    def test_failure_waits_a_full_interval_to_retry(self, d1: FakeD1) -> None:
        self._run(d1, _fail)
        calls: list[int] = []
        self._run(d1, lambda: calls.append(1))

        assert calls == []


class TestAcquireLock:
    def test_second_holder_is_refused_until_released(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(scheduling, "LOCK_DIR", tmp_path)

        first = scheduling.acquire_lock("orchestration.test")
        assert first is not None
        assert scheduling.acquire_lock("orchestration.test") is None
        # a different env's lock is independent
        other = scheduling.acquire_lock("orchestration.other")
        assert other is not None

        first.close()
        again = scheduling.acquire_lock("orchestration.test")
        assert again is not None
        again.close()
        other.close()


def _game_at(seed: Seed, kickoff: datetime, status: str | None = "SCHEDULED") -> int:
    week_id = seed.d1.query("SELECT week_id FROM weeks LIMIT 1").results
    return seed.game(
        week_id[0]["week_id"] if week_id else seed.week(1),
        game_time=_iso(kickoff),
        status=status,
    )


class TestLiveWindow:
    @pytest.mark.parametrize(
        ("started_hours_ago", "status", "live"),
        [
            (1, "IN_PROGRESS", True),
            (1, "SCHEDULED", True),  # stale status - the live poll fixes it
            (1, None, True),
            (orchestration.LIVE_WINDOW_HOURS - 0.1, "IN_PROGRESS", True),
            (orchestration.LIVE_WINDOW_HOURS + 0.1, "IN_PROGRESS", False),
            (1, "FINAL", False),
            (1, "POSTPONED", False),
            (1, "CANCELLED", False),
            (-1, "SCHEDULED", False),  # kicks off in an hour
        ],
    )
    def test_live_window(
        self,
        d1: FakeD1,
        seed: Seed,
        started_hours_ago: float,
        status: str | None,
        live: bool,
    ) -> None:
        _game_at(seed, datetime.now(UTC) - timedelta(hours=started_hours_ago), status)
        assert orchestration._is_live_window_active(d1) is live

    def test_no_games(self, d1: FakeD1) -> None:
        assert orchestration._is_live_window_active(d1) is False


class TestPreKickoffOdds:
    @pytest.fixture
    def captures(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        calls: list[str] = []
        monkeypatch.setattr(
            orchestration,
            "_capture_odds",
            lambda _client, state_key, _success_key: calls.append(state_key),
        )
        return calls

    def test_kickoff_inside_the_lead_window(
        self, d1: FakeD1, seed: Seed, captures: list[str]
    ) -> None:
        now = datetime.now(UTC)
        _game_at(seed, now + timedelta(minutes=20))
        orchestration._run_pre_kickoff_odds_capture(d1, now)
        assert captures == ["odds_prekickoff_last_call_at"]

    @pytest.mark.parametrize(
        ("minutes_out", "status"),
        [
            (orchestration.ODDS_PREKICKOFF_LEAD_MINUTES + 10, "SCHEDULED"),
            (-5, "IN_PROGRESS"),  # already kicked off
            (20, "POSTPONED"),
        ],
    )
    def test_no_capture(
        self,
        d1: FakeD1,
        seed: Seed,
        captures: list[str],
        minutes_out: int,
        status: str,
    ) -> None:
        now = datetime.now(UTC)
        _game_at(seed, now + timedelta(minutes=minutes_out), status)
        orchestration._run_pre_kickoff_odds_capture(d1, now)
        assert captures == []

    def test_same_window_doubleheader_captures_once(
        self, d1: FakeD1, seed: Seed, captures: list[str]
    ) -> None:
        # the real _capture_odds() stamps the cursor, so stamp it here too
        now = datetime.now(UTC)
        _game_at(seed, now + timedelta(minutes=20))
        orchestration._run_pre_kickoff_odds_capture(d1, now)
        scheduling.set_state(d1, "odds_prekickoff_last_call_at", _ago(0))
        _game_at(seed, now + timedelta(minutes=25))
        orchestration._run_pre_kickoff_odds_capture(d1, now)

        assert len(captures) == 1


class TestPregameWeather:
    @pytest.fixture
    def loads(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        calls: list[int] = []
        monkeypatch.setattr(
            orchestration, "load_pregame_weather", lambda: calls.append(1)
        )
        return calls

    @pytest.mark.parametrize(
        ("kickoff_hours_out", "last_capture_hours_ago", "runs"),
        [
            (3, 2, True),  # near kickoff: hourly
            (3, 0.5, False),
            (48, 2, False),  # nothing near: every 4 hours
            (48, 5, True),
        ],
    )
    def test_interval_tightens_near_kickoff(
        self,
        d1: FakeD1,
        seed: Seed,
        loads: list[int],
        kickoff_hours_out: float,
        last_capture_hours_ago: float,
        runs: bool,
    ) -> None:
        now = datetime.now(UTC)
        _game_at(seed, now + timedelta(hours=kickoff_hours_out))
        scheduling.set_state(
            d1, "weather_pregame_last_capture_at", _ago(last_capture_hours_ago * 3600)
        )

        orchestration._run_pregame_weather_capture(d1, now)

        assert loads == ([1] if runs else [])

    def test_success_and_cursor_keys(
        self, d1: FakeD1, seed: Seed, loads: list[int]
    ) -> None:
        orchestration._run_pregame_weather_capture(d1, datetime.now(UTC))

        assert loads == [1]
        assert scheduling.get_state(d1, "weather_pregame_last_capture_at")
        assert scheduling.get_state(d1, "weather_pregame_last_success_at")
