"""compute_week_leaderboard(): cumulative/half-season scores, tie-aware
places, in-money flags and per-user fields, run against the real SQL."""

from typing import Any

import pytest

from config.config import (
    PERIODS_BY_KEY,
    SEASON,
    Period,
)
from src.game_rules import standard_rank
from src.kv_writer import leaderboard
from src.kv_writer.leaderboard import compute_week_leaderboard
from tests.conftest import FakeD1, Seed

OVERALL_PAID_PLACES = PERIODS_BY_KEY["overall"].paid_places
FIRST_HALF_PAID_PLACES = PERIODS_BY_KEY["first_half"].paid_places
SECOND_HALF_PAID_PLACES = PERIODS_BY_KEY["second_half"].paid_places
SECOND_HALF_START_WEEK = PERIODS_BY_KEY["second_half"].start_week


def _by_name(board: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    assert board is not None
    return {row["name"]: row for row in board}


def _standing(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in ("score", "place", "in_money")}


def _season_scores(seed: Seed, scores: dict[str, list[int]]) -> dict[str, int]:
    """Seed one weekly_performance row per user per week, weeks numbered
    from 1. Returns user ids by name."""
    weeks = max(len(s) for s in scores.values())
    week_ids = [seed.week(n) for n in range(1, weeks + 1)]
    user_ids: dict[str, int] = {}
    for name, week_scores in scores.items():
        user_ids[name] = seed.user(name)
        for week_id, score in zip(week_ids, week_scores):
            seed.performance(user_ids[name], week_id, score)
    return user_ids


class TestStandardRank:
    def test_ties_share_a_place_and_skip_the_next(self) -> None:
        assert standard_rank({1: 10, 2: 8, 3: 8, 4: 5}) == {1: 1, 2: 2, 3: 2, 4: 4}

    def test_everyone_tied(self) -> None:
        assert standard_rank({1: 3, 2: 3, 3: 3}) == {1: 1, 2: 1, 3: 1}

    def test_empty(self) -> None:
        assert standard_rank({}) == {}


def test_no_performance_rows_returns_none(d1: FakeD1, seed: Seed) -> None:
    seed.week(1)
    assert compute_week_leaderboard(d1, 1) is None


def test_cumulative_score_and_tie_aware_place(d1: FakeD1, seed: Seed) -> None:
    _season_scores(seed, {"a": [5, 4], "b": [3, 5], "c": [4, 4], "d": [2, 1]})

    board = compute_week_leaderboard(d1, 2)
    rows = _by_name(board)

    assert {n: r["cumulative_score"] for n, r in rows.items()} == {
        "a": 9,
        "b": 8,
        "c": 8,
        "d": 3,
    }
    assert {n: r["place"] for n, r in rows.items()} == {"a": 1, "b": 2, "c": 2, "d": 4}
    # sorted by place for the UI
    assert board is not None
    assert [r["place"] for r in board] == [1, 2, 2, 4]


def test_weekly_fields_come_from_the_requested_week_only(
    d1: FakeD1, seed: Seed
) -> None:
    week1 = seed.week(1)
    week2 = seed.week(2)
    user = seed.user("a")
    seed.performance(user, week1, 5, trending_score=99, has_submitted_picks=True)
    seed.performance(user, week2, 2, trending_score=7, has_submitted_picks=False)

    row = _by_name(compute_week_leaderboard(d1, 2))["a"]

    assert row["weekly_score"] == 2
    assert row["trending_score"] == 7
    assert row["has_submitted_picks"] is False


def test_later_weeks_are_not_counted(d1: FakeD1, seed: Seed) -> None:
    _season_scores(seed, {"a": [3, 5, 5]})

    row = _by_name(compute_week_leaderboard(d1, 1))["a"]

    assert row["cumulative_score"] == 3
    assert row["weekly_score"] == 3


def test_other_seasons_are_not_counted(d1: FakeD1, seed: Seed) -> None:
    user = seed.user("a")
    seed.performance(user, seed.week(1), 4)
    seed.performance(user, seed.week(1, season_id=SEASON - 1), 5)

    row = _by_name(compute_week_leaderboard(d1, 1))["a"]

    assert row["cumulative_score"] == 4


def test_inactive_users_are_left_out(d1: FakeD1, seed: Seed) -> None:
    week = seed.week(1)
    seed.performance(seed.user("active"), week, 3)
    seed.performance(seed.user("gone", is_active=False), week, 5)

    rows = _by_name(compute_week_leaderboard(d1, 1))

    assert set(rows) == {"active"}
    assert rows["active"]["place"] == 1


def test_null_picks_correct_counts_as_zero(d1: FakeD1, seed: Seed) -> None:
    _season_scores(seed, {"a": [4]})
    user = seed.user("b")
    seed.d1.query(
        "INSERT INTO weekly_performance (user_id, week_id, picks_correct) "
        "VALUES (?, (SELECT week_id FROM weeks WHERE week_number = 1), NULL)",
        [user],
    )

    row = _by_name(compute_week_leaderboard(d1, 1))["b"]

    assert row["weekly_score"] == 0
    assert row["cumulative_score"] == 0
    assert row["place"] == 2


def test_user_missing_the_current_week_gets_zero_and_not_submitted(
    d1: FakeD1, seed: Seed
) -> None:
    week1 = seed.week(1)
    seed.week(2)
    seed.performance(seed.user("a"), week1, 4)
    seed.performance(
        seed.user("b"),
        seed.d1.query("SELECT week_id FROM weeks WHERE week_number = 2").results[0][
            "week_id"
        ],
        3,
    )

    row = _by_name(compute_week_leaderboard(d1, 2))["a"]

    assert row["weekly_score"] == 0
    assert row["trending_score"] == 0
    assert row["has_submitted_picks"] is False
    assert row["cumulative_score"] == 4


def test_seasons_played_counts_the_current_season(d1: FakeD1, seed: Seed) -> None:
    users = _season_scores(seed, {"veteran": [3], "rookie": [3]})
    seed.historical(users["veteran"], SEASON - 2)
    seed.historical(users["veteran"], SEASON - 1)

    rows = _by_name(compute_week_leaderboard(d1, 1))

    assert rows["veteran"]["seasons_played"] == 3
    assert rows["rookie"]["seasons_played"] == 1


class TestHalves:
    def test_before_second_half_it_has_no_standings(
        self, d1: FakeD1, seed: Seed
    ) -> None:
        last_first_half = SECOND_HALF_START_WEEK - 1
        _season_scores(seed, {"a": [3] * last_first_half})

        row = _by_name(compute_week_leaderboard(d1, last_first_half))["a"]

        assert row["periods"]["first_half"]["score"] == 3 * last_first_half
        assert row["periods"]["first_half"]["place"] == 1
        assert row["periods"]["second_half"]["score"] is None
        assert row["periods"]["second_half"]["place"] is None
        assert row["periods"]["second_half"]["in_money"] is False

    def test_second_half_only_counts_weeks_from_the_split(
        self, d1: FakeD1, seed: Seed
    ) -> None:
        # a leads the first half, b leads the second half
        weeks_in = SECOND_HALF_START_WEEK + 1
        first_half = SECOND_HALF_START_WEEK - 1
        _season_scores(
            seed,
            {
                "a": [5] * first_half + [1, 1],
                "b": [1] * first_half + [5, 5],
            },
        )

        rows = _by_name(compute_week_leaderboard(d1, weeks_in))

        assert rows["a"]["periods"]["first_half"]["score"] == 5 * first_half
        assert rows["a"]["periods"]["second_half"]["score"] == 2
        assert rows["b"]["periods"]["first_half"]["score"] == first_half
        assert rows["b"]["periods"]["second_half"]["score"] == 10
        assert rows["a"]["periods"]["first_half"]["place"] == 1
        assert rows["b"]["periods"]["second_half"]["place"] == 1
        assert rows["a"]["periods"]["second_half"]["place"] == 2
        # overall is the sum of both halves
        assert rows["a"]["cumulative_score"] == 5 * first_half + 2
        assert rows["b"]["cumulative_score"] == first_half + 10

    def test_first_half_is_frozen_once_the_second_half_starts(
        self, d1: FakeD1, seed: Seed
    ) -> None:
        _season_scores(seed, {"a": [2] * (SECOND_HALF_START_WEEK + 2)})

        at_split = _by_name(compute_week_leaderboard(d1, SECOND_HALF_START_WEEK))
        later = _by_name(compute_week_leaderboard(d1, SECOND_HALF_START_WEEK + 2))

        assert (
            at_split["a"]["periods"]["first_half"]["score"]
            == later["a"]["periods"]["first_half"]["score"]
        )
        assert at_split["a"]["periods"]["second_half"]["score"] == 2
        assert later["a"]["periods"]["second_half"]["score"] == 6

    def test_user_joining_in_the_second_half(self, d1: FakeD1, seed: Seed) -> None:
        week_ids = [seed.week(n) for n in range(1, SECOND_HALF_START_WEEK + 1)]
        seed.performance(seed.user("late"), week_ids[-1], 4)

        row = _by_name(compute_week_leaderboard(d1, SECOND_HALF_START_WEEK))["late"]

        assert row["periods"]["first_half"]["score"] is None
        assert row["periods"]["first_half"]["place"] is None
        assert row["periods"]["first_half"]["in_money"] is False
        assert row["periods"]["second_half"]["score"] == 4
        assert row["periods"]["second_half"]["place"] == 1


class TestPeriods:
    THIRDS = (
        Period("overall", "Overall", 1, None, paid_places=2),
        Period("first_third", "First Third", 1, 2, paid_places=1),
        Period("second_third", "Second Third", 3, 4, paid_places=1),
        Period("third_third", "Third Third", 5, None, paid_places=1),
    )

    def test_any_period_structure(
        self, d1: FakeD1, seed: Seed, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(leaderboard, "PERIODS", self.THIRDS)
        _season_scores(seed, {"a": [5, 5, 1, 1], "b": [1, 1, 5, 4], "c": [3, 3, 3, 3]})

        rows = _by_name(compute_week_leaderboard(d1, 4))

        assert _standing(rows["a"]["periods"]["first_third"]) == {
            "score": 10,
            "place": 1,
            "in_money": True,
        }
        assert _standing(rows["b"]["periods"]["second_third"]) == {
            "score": 9,
            "place": 1,
            "in_money": True,
        }
        assert _standing(rows["c"]["periods"]["overall"]) == {
            "score": 12,
            "place": 1,
            "in_money": True,
        }
        # not started yet
        assert _standing(rows["a"]["periods"]["third_third"]) == {
            "score": None,
            "place": None,
            "in_money": False,
        }


class TestLastPlace:
    PAID = (
        Period("overall", "Overall", 1, None, paid_places=1, pay_last_place=True),
        Period("first_third", "First Third", 1, 2, paid_places=1, pay_last_place=True),
        Period("rest", "Rest", 3, None, paid_places=1, pay_last_place=True),
    )

    @pytest.fixture(autouse=True)
    def _paid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(leaderboard, "PERIODS", self.PAID)

    def _week(self, seed: Seed, week_number: int, complete: bool = True) -> int:
        week_id = seed.week(week_number)
        seed.d1.query(
            "UPDATE weeks SET is_complete = ? WHERE week_id = ?", [complete, week_id]
        )
        return week_id

    def test_lowest_eligible_score_wins(self, d1: FakeD1, seed: Seed) -> None:
        weeks = [self._week(seed, n) for n in (1, 2)]
        users = {name: seed.user(name) for name in ("top", "low", "skipped")}
        for week_id in weeks:
            seed.performance(users["top"], week_id, 4)
            seed.performance(users["low"], week_id, 1)
        # the lowest score, but one week short a pick
        seed.performance(users["skipped"], weeks[0], 0, picks_made=5)
        seed.performance(users["skipped"], weeks[1], 0, picks_made=4)

        rows = _by_name(compute_week_leaderboard(d1, 2))
        overall = {name: row["periods"]["overall"] for name, row in rows.items()}

        assert overall["skipped"]["last_place_eligible"] is False
        assert overall["skipped"]["in_money_last_place"] is False
        assert overall["low"]["last_place_eligible"] is True
        assert overall["low"]["in_money_last_place"] is True
        assert overall["top"]["in_money_last_place"] is False

    def test_a_missing_week_row_is_not_eligible(self, d1: FakeD1, seed: Seed) -> None:
        week1, week2 = self._week(seed, 1), self._week(seed, 2)
        late = seed.user("late")
        seed.performance(seed.user("a"), week1, 3)
        seed.performance(seed.user("a2"), week2, 3)
        seed.performance(late, week2, 0)

        row = _by_name(compute_week_leaderboard(d1, 2))["late"]

        assert row["periods"]["overall"]["last_place_eligible"] is False

    def test_unfinished_week_does_not_count(self, d1: FakeD1, seed: Seed) -> None:
        # before the deadline only kicked-off picks are loaded
        week = self._week(seed, 1, complete=False)
        seed.performance(seed.user("a"), week, 0, picks_made=2)
        seed.performance(seed.user("b"), week, 1, picks_made=3)

        overall = _by_name(compute_week_leaderboard(d1, 1))["a"]["periods"]["overall"]

        assert overall["last_place_eligible"] is True
        assert overall["in_money_last_place"] is True

    def test_tied_for_last_both_paid(self, d1: FakeD1, seed: Seed) -> None:
        week = self._week(seed, 1)
        for name, score in (("a", 1), ("b", 1), ("c", 4)):
            seed.performance(seed.user(name), week, score)

        rows = _by_name(compute_week_leaderboard(d1, 1))

        paid = {
            n for n, r in rows.items() if r["periods"]["overall"]["in_money_last_place"]
        }
        assert paid == {"a", "b"}

    def test_each_period_judged_on_its_own_weeks(self, d1: FakeD1, seed: Seed) -> None:
        weeks = [self._week(seed, n) for n in (1, 2, 3)]
        a, b = seed.user("a"), seed.user("b")
        for week_id in weeks:
            seed.performance(b, week_id, 3)
        # a skips week 1, then picks lowest in week 3
        seed.performance(a, weeks[0], 0, picks_made=0)
        seed.performance(a, weeks[1], 5)
        seed.performance(a, weeks[2], 0)

        periods = _by_name(compute_week_leaderboard(d1, 3))["a"]["periods"]

        assert periods["overall"]["last_place_eligible"] is False
        assert periods["first_third"]["last_place_eligible"] is False
        assert periods["rest"]["last_place_eligible"] is True
        assert periods["rest"]["in_money_last_place"] is True

    def test_before_the_period_starts(self, d1: FakeD1, seed: Seed) -> None:
        seed.performance(seed.user("a"), self._week(seed, 1), 2)

        rest = _by_name(compute_week_leaderboard(d1, 1))["a"]["periods"]["rest"]

        assert rest["last_place_eligible"] is None
        assert rest["in_money_last_place"] is False

    def test_not_paid_unless_the_period_says_so(
        self, d1: FakeD1, seed: Seed, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            leaderboard, "PERIODS", (Period("overall", "Overall", 1, None, 1),)
        )
        week = self._week(seed, 1)
        seed.performance(seed.user("a"), week, 0)
        seed.performance(seed.user("b"), week, 5)

        overall = _by_name(compute_week_leaderboard(d1, 1))["a"]["periods"]["overall"]

        assert overall["last_place_eligible"] is True
        assert overall["in_money_last_place"] is False


class TestInMoney:
    def test_overall_paid_places_cutoff(self, d1: FakeD1, seed: Seed) -> None:
        # distinct scores, one more user than paid places
        count = OVERALL_PAID_PLACES + 1
        _season_scores(seed, {f"u{i}": [count - i] for i in range(count)})

        rows = _by_name(compute_week_leaderboard(d1, 1))

        paid = {n for n, r in rows.items() if r["periods"]["overall"]["in_money"]}
        assert paid == {f"u{i}" for i in range(OVERALL_PAID_PLACES)}

    def test_tie_at_the_cutoff_pays_everyone_tied(self, d1: FakeD1, seed: Seed) -> None:
        # places 1..(N-1) distinct, then three users tied for place N
        above = OVERALL_PAID_PLACES - 1
        scores = {f"top{i}": [100 - i] for i in range(above)}
        scores |= {"tie1": [10], "tie2": [10], "tie3": [10], "last": [1]}
        _season_scores(seed, scores)

        rows = _by_name(compute_week_leaderboard(d1, 1))

        for name in ("tie1", "tie2", "tie3"):
            assert rows[name]["place"] == OVERALL_PAID_PLACES
            assert rows[name]["periods"]["overall"]["in_money"] is True
        assert rows["last"]["place"] == OVERALL_PAID_PLACES + 3
        assert rows["last"]["periods"]["overall"]["in_money"] is False

    def test_half_season_paid_places(self, d1: FakeD1, seed: Seed) -> None:
        first_half = SECOND_HALF_START_WEEK - 1
        count = max(FIRST_HALF_PAID_PLACES, SECOND_HALF_PAID_PLACES) + 1
        # first half ranks u0 highest, second half ranks it lowest
        _season_scores(
            seed,
            {f"u{i}": [count - i] * first_half + [i + 1] for i in range(count)},
        )

        rows = _by_name(compute_week_leaderboard(d1, SECOND_HALF_START_WEEK))

        first_paid = {
            n for n, r in rows.items() if r["periods"]["first_half"]["in_money"]
        }
        second_paid = {
            n for n, r in rows.items() if r["periods"]["second_half"]["in_money"]
        }
        assert first_paid == {f"u{i}" for i in range(FIRST_HALF_PAID_PLACES)}
        assert second_paid == {
            f"u{i}" for i in range(count - SECOND_HALF_PAID_PLACES, count)
        }


class TestPicks:
    def test_picks_for_the_requested_week_only(self, d1: FakeD1, seed: Seed) -> None:
        week1 = seed.week(1)
        week2 = seed.week(2)
        user = seed.user("a")
        seed.performance(user, week1, 1)
        seed.performance(user, week2, 1)
        old_game = seed.game(week1)
        game = seed.game(week2)
        team = seed.team("KC")
        seed.pick(user, old_game, team, is_correct=False)
        seed.pick(user, game, team, is_correct=True, trending_status="HOT")

        row = _by_name(compute_week_leaderboard(d1, 2))["a"]

        assert row["picks"] == [
            {
                "game_id": game,
                "team_id": team,
                "is_correct": True,
                "trending_status": "HOT",
            }
        ]

    @pytest.mark.parametrize(
        ("stored", "expected"), [(None, None), (True, True), (False, False)]
    )
    def test_is_correct_stays_a_bool_or_none(
        self, d1: FakeD1, seed: Seed, stored: bool | None, expected: bool | None
    ) -> None:
        week = seed.week(1)
        user = seed.user("a")
        seed.performance(user, week, 0)
        seed.pick(user, seed.game(week), seed.team(), is_correct=stored)

        pick = _by_name(compute_week_leaderboard(d1, 1))["a"]["picks"][0]

        assert pick["is_correct"] is expected

    def test_user_with_no_picks_gets_an_empty_list(
        self, d1: FakeD1, seed: Seed
    ) -> None:
        _season_scores(seed, {"a": [0]})

        assert _by_name(compute_week_leaderboard(d1, 1))["a"]["picks"] == []
