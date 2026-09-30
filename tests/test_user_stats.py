"""src/user_stats.py - per-user profile stats, plus the career record it's
fed from kv_writer/historical.py. Pure helpers are tested on hand-built
rows, compute_user_profiles() against the real SQL."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from config.config import SEASON, SECOND_HALF_START_WEEK
from src import user_stats as us
from src.kv_writer.historical import career_record_by_user
from tests.conftest import FakeD1, Seed

KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)


def _pick_row(
    picked: int,
    correct: bool | None,
    week: int = 1,
    home: int = 1,
    away: int = 2,
    spread: float | None = -3.0,
    game_id: int = 1,
) -> dict[str, Any]:
    """one _SEASON_USER_PICKS_SQL row"""
    return {
        "game_id": game_id,
        "week_number": week,
        "home_team_id": home,
        "away_team_id": away,
        "cbs_spread": spread,
        "picked_team_id": picked,
        "is_correct": correct,
        "team_abbr": f"T{picked}",
        "team_name": f"Team {picked}",
        "home_abbr": f"T{home}",
        "home_name": f"Team {home}",
        "away_abbr": f"T{away}",
        "away_name": f"Team {away}",
    }


def _week_row(week: int, correct: int, made: int = 5) -> dict[str, Any]:
    return {"week_number": week, "picks_made": made, "picks_correct": correct}


class TestStreakHelpers:
    @pytest.mark.parametrize(
        ("flags", "expected"),
        [
            ([], (0, 0)),
            ([True, True, False, True], (1, 2)),
            ([True, True, True], (3, 3)),
            ([True, False], (0, 1)),
        ],
    )
    def test_current_and_longest(
        self, flags: list[bool], expected: tuple[int, int]
    ) -> None:
        assert us._current_and_longest_streak(flags) == expected

    @pytest.mark.parametrize(
        ("weeks", "latest", "expected"),
        [
            ([1, 2, 3, 5], 5, (3, 1)),
            ([1, 2, 3], 3, (3, 3)),
            ([1, 2, 3], 4, (3, 0)),  # skipped the latest week - streak is over
            ([2, 2, 3], 3, (2, 2)),  # two picks of a team in one week count once
        ],
    )
    def test_consecutive_week_runs(
        self, weeks: list[int], latest: int, expected: tuple[int, int]
    ) -> None:
        assert us._consecutive_week_runs(weeks, latest) == expected

    def test_hot_streak_threshold_is_80_pct(self) -> None:
        weeks = [_week_row(1, 5), _week_row(2, 4), _week_row(3, 3), _week_row(4, 4)]
        hot = us._hot_streak(weeks)
        assert (hot["current_streak"], hot["longest_streak"]) == (1, 2)

    def test_hot_streak_skips_weeks_with_no_picks(self) -> None:
        hot = us._hot_streak([_week_row(1, 4), _week_row(2, 0, made=0)])
        assert hot["current_streak"] == 1


class TestFavorite:
    @pytest.mark.parametrize(
        ("spread", "is_home", "expected"),
        [
            (-3.0, True, True),  # home favored, picked home
            (-3.0, False, False),
            (3.0, False, True),  # away favored, picked away
            (3.0, True, False),
            (0.0, True, None),  # pick'em
            (None, True, None),
        ],
    )
    def test_is_favorite(
        self, spread: float | None, is_home: bool, expected: bool | None
    ) -> None:
        assert us._is_favorite({"cbs_spread": spread}, is_home) is expected

    def test_pick_bias(self) -> None:
        rows = [
            _pick_row(1, True, spread=-3.0),  # home favorite
            _pick_row(1, False, spread=-3.0),  # home favorite
            _pick_row(2, True, spread=-3.0),  # away underdog
            _pick_row(2, True, spread=0.0),  # away, pick'em - no fav/dog
        ]
        bias = us._pick_bias(rows)
        assert bias["home"] == {"pct": 0.5, "picks": 4}
        assert bias["away"] == {"pct": 0.5, "picks": 4}
        assert bias["favorite"] == {"pct": 0.667, "picks": 3}
        assert bias["underdog"] == {"pct": 0.333, "picks": 3}


class TestPickRecords:
    def test_by_side(self) -> None:
        rows = [
            _pick_row(1, True, spread=-3.0),  # home favorite
            _pick_row(1, False, spread=-3.0),  # home favorite
            _pick_row(2, True, spread=-3.0),  # away underdog
            _pick_row(2, None, spread=0.0),  # away, pick'em, push/not played
        ]
        records = us._pick_records(rows)
        assert records["home"] == {"picks": 2, "wins": 1, "losses": 1, "win_pct": 0.5}
        assert records["away"] == {"picks": 2, "wins": 1, "losses": 0, "win_pct": 1.0}
        assert records["favorite"] == {
            "picks": 2,
            "wins": 1,
            "losses": 1,
            "win_pct": 0.5,
        }
        assert records["underdog"] == {
            "picks": 1,
            "wins": 1,
            "losses": 0,
            "win_pct": 1.0,
        }

    def test_side_with_no_picks(self) -> None:
        records = us._pick_records([_pick_row(1, True)])
        assert records["away"] == {
            "picks": 0,
            "wins": 0,
            "losses": 0,
            "win_pct": None,
        }

    def test_every_team_picked_and_against(self) -> None:
        rows = [
            _pick_row(1, True, home=1, away=2),  # took 1 over 2
            _pick_row(3, False, home=1, away=3),  # took 3 over 1
            _pick_row(2, None, home=2, away=4),  # took 2 over 4, not graded
        ]
        teams = {t["team"]["id"]: t for t in us._pick_records(rows)["teams"]}

        # a single pick still counts - no floor, it's just the record
        assert list(teams) == [1, 2, 3, 4]  # sorted by abbreviation
        assert teams[1]["team"] == {"id": 1, "abbr": "T1", "name": "Team 1"}
        assert teams[1]["picked"] == {
            "picks": 1,
            "wins": 1,
            "losses": 0,
            "win_pct": 1.0,
        }
        assert teams[1]["against"] == {
            "picks": 1,
            "wins": 0,
            "losses": 1,
            "win_pct": 0.0,
        }
        assert teams[2]["picked"]["picks"] == 1
        assert teams[2]["picked"]["win_pct"] is None
        assert teams[2]["against"]["wins"] == 1
        assert teams[4]["picked"]["picks"] == 0
        assert teams[4]["against"]["picks"] == 1


def _records(*results: tuple[int, int, int]) -> dict[int, dict[str, Any]]:
    """(team_id, wins, losses) -> _team_records() shape"""
    return {
        team_id: {
            "team": {"id": team_id},
            "wins": wins,
            "losses": losses,
            "win_pct": round(wins / (wins + losses), 3),
        }
        for team_id, wins, losses in results
    }


class TestTeamHabits:
    def test_team_records_need_two_graded_picks(self) -> None:
        rows = [
            _pick_row(1, True),
            _pick_row(1, False),
            _pick_row(1, None),  # ungraded - left out
            _pick_row(2, True, home=2, away=3),
        ]
        records = us._team_records(rows)
        assert set(records) == {1}
        assert (records[1]["wins"], records[1]["losses"]) == (1, 1)

    def test_trap_team_is_a_habit_not_a_rate(self) -> None:
        # 2-8 on a team picked ten times beats 0-2 on one picked twice
        records = _records((1, 2, 8), (2, 0, 2))
        trap = us._trap_team(records)
        assert trap is not None and trap["team"]["id"] == 1
        assert trap["pct_of_picks"] == round(10 / 12, 3)

    def test_winning_team_is_never_a_trap(self) -> None:
        assert us._trap_team(_records((1, 2, 0))) is None
        assert us._lucky_team(_records((1, 2, 0)))["team"]["id"] == 1

    def test_even_record_is_neither(self) -> None:
        records = _records((1, 3, 3))
        assert us._trap_team(records) is None
        assert us._lucky_team(records) is None

    def test_empty(self) -> None:
        assert us._trap_team({}) is None
        assert us._lucky_team({}) is None

    def test_readability_counts_picking_and_fading_the_same(self) -> None:
        # picked 1 over 2 (won), then picked 3 over 1 (won) - reads team 1
        # right both times, from either side
        rows = [
            _pick_row(1, True, home=1, away=2, game_id=1),
            _pick_row(3, True, home=3, away=1, game_id=2),
        ]
        readability = us._team_readability(rows)
        assert readability[1]["picks"] == 2
        assert readability[1]["accuracy"] == 1.0
        assert 2 not in readability and 3 not in readability  # one pick each

    def test_blind_and_sweet_spot(self) -> None:
        readability = {
            1: {"team": {"id": 1}, "picks": 4, "accuracy": 0.25},
            2: {"team": {"id": 2}, "picks": 2, "accuracy": 0.25},
            3: {"team": {"id": 3}, "picks": 3, "accuracy": 0.667},
        }
        blind, sweet = us._blind_spot_and_sweet_spot(readability)
        assert blind is not None and blind["team"]["id"] == 1  # tie -> more picks
        assert sweet is not None and sweet["team"]["id"] == 3

    def test_even_readability_is_neither(self) -> None:
        readability = {1: {"team": {"id": 1}, "picks": 2, "accuracy": 0.5}}
        assert us._blind_spot_and_sweet_spot(readability) == (None, None)


class TestContrarian:
    def test_against_and_with_the_pool(self) -> None:
        pool = [
            _pick_row(1, True, game_id=1),
            _pick_row(1, True, game_id=1),
            _pick_row(2, False, game_id=1),
            _pick_row(3, None, home=3, away=4, game_id=2),
            _pick_row(4, None, home=3, away=4, game_id=2),  # a tie - skipped
        ]
        sides = us._game_side_pick_counts(pool)
        assert sides == {1: (2, 1), 2: (1, 1)}

        loner = us._contrarian_block([pool[2], pool[3]], sides)
        assert loner == {
            "contrarian_picks": 1,
            "contrarian_accuracy_pct": 0.0,
            "chalk_picks": 0,
            "chalk_accuracy_pct": None,
        }
        chalk = us._contrarian_block([pool[0]], sides)
        assert chalk["chalk_picks"] == 1
        assert chalk["chalk_accuracy_pct"] == 1.0

    def test_ungraded_picks_count_but_have_no_accuracy(self) -> None:
        pool = [_pick_row(1, None), _pick_row(1, None), _pick_row(2, None)]
        block = us._contrarian_block(pool[:1], us._game_side_pick_counts(pool))
        assert block["chalk_picks"] == 1
        assert block["chalk_accuracy_pct"] is None


class TestWeeklyHelpers:
    def test_best_and_worst_week(self) -> None:
        best, worst = us._best_and_worst_week(
            [_week_row(1, 3), _week_row(2, 5), _week_row(3, 1)]
        )
        assert best == {"week_number": 2, "score": 5}
        assert worst == {"week_number": 3, "score": 1}
        assert us._best_and_worst_week([]) == (None, None)

    def test_consistency_needs_two_weeks(self) -> None:
        assert us._consistency([_week_row(1, 3)]) is None
        assert us._consistency([_week_row(1, 1), _week_row(2, 5)]) == {
            "stddev": 2.0,
            "weeks_counted": 2,
        }

    def test_clutch_uses_money_weeks(self) -> None:
        weeks = [_week_row(1, 1), _week_row(9, 5), _week_row(18, 4)]
        clutch = us._clutch(weeks, {9, 18})
        assert clutch == {
            "money_week_accuracy_pct": 0.9,
            "season_accuracy_pct": round(10 / 15, 3),
            "money_weeks_counted": 2,
        }


def _history(season: int, rank: int, incomplete: bool = False) -> dict[str, Any]:
    return {
        "season": season,
        "rank": rank,
        "score": 100 - rank,
        "incomplete": incomplete,
    }


class TestSeasonTrend:
    @pytest.mark.parametrize(
        ("ranks", "direction", "change"),
        [((19, 2), "improving", -17), ((2, 19), "declining", 17), ((4, 4), "same", 0)],
    )
    def test_direction(
        self, ranks: tuple[int, int], direction: str, change: int
    ) -> None:
        trend = us._season_trend([_history(2024, ranks[0]), _history(2025, ranks[1])])
        assert trend is not None
        assert (trend["direction"], trend["rank_change"]) == (direction, change)
        assert trend["last_season"]["season"] == 2025

    def test_needs_two_seasons(self) -> None:
        assert us._season_trend([_history(2025, 1)]) is None

    def test_incomplete_seasons_are_skipped(self) -> None:
        trend = us._season_trend(
            [_history(2015, 1), _history(2016, 30, incomplete=True), _history(2017, 5)]
        )
        assert trend is not None
        assert trend["prior_season"]["season"] == 2015


class TestCareerRecord:
    def test_titles_best_finish_and_history(self, d1: FakeD1, seed: Seed) -> None:
        user = seed.user("a")
        seed.historical(user, 2023, final_rank=4, final_score=60)
        seed.historical(user, 2024, final_rank=1, final_score=80)
        seed.historical(user, 2025, final_rank=1, final_score=75)

        career = career_record_by_user(d1)[user]

        assert career["appearances"] == [2023, 2024, 2025]
        assert career["titles"] == 2
        assert career["best_finish"] == 1
        assert career["best_finish_years"] == [2024, 2025]
        assert [s["season"] for s in career["season_history"]] == [2023, 2024, 2025]

    def test_incomplete_season_is_flagged(self, d1: FakeD1, seed: Seed) -> None:
        user = seed.user("a")
        seed.historical(user, 2016, final_rank=3)
        d1.query(
            "UPDATE seasons SET historical_data_incomplete = 1 WHERE season_id = 2016"
        )

        history = career_record_by_user(d1)[user]["season_history"]

        assert history[0]["incomplete"] is True


class Pool:
    """A small season: seeded users, teams and one game per (week, pairing)"""

    def __init__(self, seed: Seed) -> None:
        self.seed = seed
        self.users: dict[str, int] = {}
        self.teams: dict[str, int] = {}
        self.weeks: dict[int, int] = {}

    def user(self, name: str, **values: Any) -> int:
        if name not in self.users:
            self.users[name] = self.seed.user(name, **values)
        return self.users[name]

    def team(self, abbr: str) -> int:
        if abbr not in self.teams:
            self.teams[abbr] = self.seed.team(abbr)
        return self.teams[abbr]

    def week(self, number: int, complete: bool = True) -> int:
        if number not in self.weeks:
            self.weeks[number] = self.seed.week(number)
            self.seed.d1.query(
                "UPDATE weeks SET is_complete = ? WHERE week_id = ?",
                [complete, self.weeks[number]],
            )
        return self.weeks[number]

    def game(self, week: int, home: str, away: str, spread: float | None = -3.0) -> int:
        return self.seed.game(
            self.week(week),
            home_team_id=self.team(home),
            away_team_id=self.team(away),
            cbs_spread=spread,
            game_time=KICKOFF + timedelta(weeks=week - 1, hours=len(self.teams)),
        )

    def pick(self, user: str, game_id: int, team: str, correct: bool | None) -> None:
        self.seed.pick(self.user(user), game_id, self.team(team), is_correct=correct)

    def score(self, user: str, week: int, correct: int, made: int = 5) -> None:
        self.seed.performance(
            self.user(user), self.week(week), correct, picks_made=made
        )


def _profiles(d1: FakeD1) -> dict[str, dict[str, Any]]:
    profiles = us.compute_user_profiles(d1, career_record_by_user(d1))
    return {p["name"]: p for p in profiles.values()}


class TestComputeUserProfiles:
    def test_season_totals_sum_every_week(self, d1: FakeD1, seed: Seed) -> None:
        # total_picks once read 6 across two weeks instead of 10, from a
        # stale picks_made (see CLAUDE.local.md's user_stats entry)
        pool = Pool(seed)
        pool.score("a", 1, 4)
        pool.score("a", 2, 3)

        season = _profiles(d1)["a"]["current_season"]

        assert season["total_picks"] == 10
        assert season["total_correct"] == 7
        assert season["accuracy_pct"] == 0.7

    def test_current_rank_is_tie_aware(self, d1: FakeD1, seed: Seed) -> None:
        pool = Pool(seed)
        for name, score in (("a", 5), ("b", 3), ("c", 3), ("d", 1)):
            pool.score(name, 1, score)

        ranks = {
            n: p["current_season"]["current_rank"] for n, p in _profiles(d1).items()
        }

        assert ranks == {"a": 1, "b": 2, "c": 2, "d": 4}

    def test_only_active_users_get_a_profile(self, d1: FakeD1, seed: Seed) -> None:
        pool = Pool(seed)
        pool.user("gone", is_active=False)
        pool.score("gone", 1, 5)
        pool.score("a", 1, 1)

        profiles = _profiles(d1)

        assert set(profiles) == {"a"}
        assert profiles["a"]["current_season"]["current_rank"] == 1

    def test_user_with_no_picks_yet(self, d1: FakeD1, seed: Seed) -> None:
        Pool(seed).user("new")

        season = _profiles(d1)["new"]["current_season"]

        assert season["total_picks"] == 0
        assert season["accuracy_pct"] is None
        assert season["current_rank"] is None
        for block in (
            "team_pick_streak",
            "pick_bias",
            "records",
            "contrarian",
            "trap_team",
        ):
            assert season[block] is None

    def test_career_block(self, d1: FakeD1, seed: Seed) -> None:
        pool = Pool(seed)
        user = pool.user("vet")
        seed.historical(user, 2024, final_rank=19)
        seed.historical(user, 2025, final_rank=2)
        pool.user("rookie")

        profiles = _profiles(d1)

        vet = profiles["vet"]["career"]
        assert vet["years_played"] == 3  # two prior seasons plus this one
        assert vet["trend"]["direction"] == "improving"
        rookie = profiles["rookie"]["career"]
        assert rookie["years_played"] == 1
        assert rookie["titles"] == 0
        assert rookie["trend"] is None

    def test_team_pick_streak_across_weeks(self, d1: FakeD1, seed: Seed) -> None:
        pool = Pool(seed)
        for week in (1, 2, 3):
            pool.pick("a", pool.game(week, "KC", f"OPP{week}"), "KC", True)
        pool.pick("a", pool.game(4, "NYJ", "MIA"), "NYJ", False)

        streak = _profiles(d1)["a"]["current_season"]["team_pick_streak"]

        # KC's run ended when week 4 went to someone else
        assert streak["current"] is None
        assert streak["longest"]["team"]["abbr"] == "KC"
        assert streak["longest"]["weeks"] == 3
        assert streak["most_picked_team"] == {
            "team": {"id": pool.teams["KC"], "abbr": "KC", "name": "Kc"},
            "count": 3,
        }

    def test_contrarian_is_judged_against_the_whole_pool(
        self, d1: FakeD1, seed: Seed
    ) -> None:
        pool = Pool(seed)
        game = pool.game(1, "KC", "BUF")
        pool.pick("a", game, "KC", False)
        pool.pick("b", game, "KC", False)
        pool.pick("c", game, "BUF", True)

        profiles = _profiles(d1)

        assert profiles["c"]["current_season"]["contrarian"]["contrarian_picks"] == 1
        assert (
            profiles["c"]["current_season"]["contrarian"]["contrarian_accuracy_pct"]
            == 1.0
        )
        assert profiles["a"]["current_season"]["contrarian"]["chalk_picks"] == 1

    def test_week_stats_only_count_complete_weeks(self, d1: FakeD1, seed: Seed) -> None:
        pool = Pool(seed)
        pool.week(1, complete=True)
        pool.week(2, complete=False)
        pool.score("a", 1, 4)
        pool.score("a", 2, 0)  # in progress - not the worst week yet

        season = _profiles(d1)["a"]["current_season"]

        assert season["worst_week"] == {"week_number": 1, "score": 4}
        assert season["hot_streak"]["current_streak"] == 1
        assert season["consistency"] is None
        # the rank still counts the week in progress, like the live leaderboard
        assert season["total_correct"] == 4

    def test_clutch_money_weeks(self, d1: FakeD1, seed: Seed) -> None:
        # the week before the second half starts, and the season's last week
        pool = Pool(seed)
        last = SECOND_HALF_START_WEEK + 2
        pool.score("a", 1, 1)
        pool.score("a", SECOND_HALF_START_WEEK - 1, 5)
        pool.score("a", last, 4)

        clutch = _profiles(d1)["a"]["current_season"]["clutch"]

        assert clutch["money_weeks_counted"] == 2
        assert clutch["money_week_accuracy_pct"] == 0.9

    def test_profile_is_for_this_season(self, d1: FakeD1, seed: Seed) -> None:
        pool = Pool(seed)
        pool.score("a", 1, 3)
        old_week = seed.week(1, season_id=SEASON - 1)
        seed.performance(pool.users["a"], old_week, 5)

        profile = _profiles(d1)["a"]

        assert profile["season"] == SEASON
        assert profile["current_season"]["total_correct"] == 3
