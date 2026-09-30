"""compute_week_leaderboard(): cumulative/half-season scores, tie-aware
places, in-money flags and per-user fields, run against the real SQL."""

from typing import Any

import pytest

from config.config import (
    FIRST_HALF_PAID_PLACES,
    OVERALL_PAID_PLACES,
    SEASON,
    SECOND_HALF_PAID_PLACES,
    SECOND_HALF_START_WEEK,
)
from src.game_rules import standard_rank
from src.kv_writer.leaderboard import compute_week_leaderboard
from tests.conftest import FakeD1, Seed


def _by_name(board: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    assert board is not None
    return {row["name"]: row for row in board}


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
    def test_before_second_half_has_no_second_half_fields(
        self, d1: FakeD1, seed: Seed
    ) -> None:
        last_first_half = SECOND_HALF_START_WEEK - 1
        _season_scores(seed, {"a": [3] * last_first_half})

        row = _by_name(compute_week_leaderboard(d1, last_first_half))["a"]

        assert row["first_half_score"] == 3 * last_first_half
        assert row["first_half_place"] == 1
        assert row["second_half_score"] is None
        assert row["second_half_place"] is None
        assert row["in_money_second_half"] is False

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

        assert rows["a"]["first_half_score"] == 5 * first_half
        assert rows["a"]["second_half_score"] == 2
        assert rows["b"]["first_half_score"] == first_half
        assert rows["b"]["second_half_score"] == 10
        assert rows["a"]["first_half_place"] == 1
        assert rows["b"]["second_half_place"] == 1
        assert rows["a"]["second_half_place"] == 2
        # overall is the sum of both halves
        assert rows["a"]["cumulative_score"] == 5 * first_half + 2
        assert rows["b"]["cumulative_score"] == first_half + 10

    def test_first_half_is_frozen_once_the_second_half_starts(
        self, d1: FakeD1, seed: Seed
    ) -> None:
        _season_scores(seed, {"a": [2] * (SECOND_HALF_START_WEEK + 2)})

        at_split = _by_name(compute_week_leaderboard(d1, SECOND_HALF_START_WEEK))
        later = _by_name(compute_week_leaderboard(d1, SECOND_HALF_START_WEEK + 2))

        assert at_split["a"]["first_half_score"] == later["a"]["first_half_score"]
        assert at_split["a"]["second_half_score"] == 2
        assert later["a"]["second_half_score"] == 6

    def test_user_joining_in_the_second_half(self, d1: FakeD1, seed: Seed) -> None:
        week_ids = [seed.week(n) for n in range(1, SECOND_HALF_START_WEEK + 1)]
        seed.performance(seed.user("late"), week_ids[-1], 4)

        row = _by_name(compute_week_leaderboard(d1, SECOND_HALF_START_WEEK))["late"]

        assert row["first_half_score"] is None
        assert row["first_half_place"] is None
        assert row["in_money_first_half"] is False
        assert row["second_half_score"] == 4
        assert row["second_half_place"] == 1


class TestInMoney:
    def test_overall_paid_places_cutoff(self, d1: FakeD1, seed: Seed) -> None:
        # distinct scores, one more user than paid places
        count = OVERALL_PAID_PLACES + 1
        _season_scores(seed, {f"u{i}": [count - i] for i in range(count)})

        rows = _by_name(compute_week_leaderboard(d1, 1))

        paid = {n for n, r in rows.items() if r["in_money_overall"]}
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
            assert rows[name]["in_money_overall"] is True
        assert rows["last"]["place"] == OVERALL_PAID_PLACES + 3
        assert rows["last"]["in_money_overall"] is False

    def test_half_season_paid_places(self, d1: FakeD1, seed: Seed) -> None:
        first_half = SECOND_HALF_START_WEEK - 1
        count = max(FIRST_HALF_PAID_PLACES, SECOND_HALF_PAID_PLACES) + 1
        # first half ranks u0 highest, second half ranks it lowest
        _season_scores(
            seed,
            {f"u{i}": [count - i] * first_half + [i + 1] for i in range(count)},
        )

        rows = _by_name(compute_week_leaderboard(d1, SECOND_HALF_START_WEEK))

        first_paid = {n for n, r in rows.items() if r["in_money_first_half"]}
        second_paid = {n for n, r in rows.items() if r["in_money_second_half"]}
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
