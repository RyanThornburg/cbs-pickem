"""src/kv_writer/recap.py - the weekly recap key. Helpers are tested
directly, each recap item kind against a seeded scenario through
compute_week_recap() and the real SQL."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from config.config import SEASON
from src.kv_writer import recap
from src.kv_writer.shared import ats_side
from tests.conftest import Clients, FakeD1, Seed, iso

# Sunday 1pm ET of week 1; week N is 7 days later per week
WEEK1_SUNDAY = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)


class Slate:
    """Seeds a season one game at a time. Picks are graded from the game's
    own score and spread (CBS's is_correct, as the loader would store it)."""

    def __init__(self, seed: Seed) -> None:
        self.seed = seed
        self.users: dict[str, int] = {}
        self.teams: dict[str, int] = {}
        self.weeks: dict[int, int] = {}
        self._games: dict[int, dict[str, Any]] = {}

    def user(self, name: str) -> int:
        if name not in self.users:
            self.users[name] = self.seed.user(name)
        return self.users[name]

    def team(self, abbr: str, division: str | None = None) -> int:
        if abbr not in self.teams:
            self.teams[abbr] = self.seed.team(abbr, division=division)
        return self.teams[abbr]

    def week(self, number: int) -> int:
        if number not in self.weeks:
            self.weeks[number] = self.seed.week(number)
        return self.weeks[number]

    def game(
        self,
        week: int,
        home: str,
        away: str,
        spread: float | None = -3.0,
        score: tuple[int, int] | None = None,
        kickoff: datetime | None = None,
        **values: Any,
    ) -> int:
        """`score` None means not played yet"""
        kickoff = kickoff or WEEK1_SUNDAY + timedelta(
            weeks=week - 1, minutes=len(self._games)
        )
        game = {
            "home_id": self.team(home),
            "away_id": self.team(away),
            "cbs_spread": spread,
            "status": "FINAL" if score else "SCHEDULED",
            "home_score": score[0] if score else None,
            "away_score": score[1] if score else None,
        }
        game_id = self.seed.game(
            self.week(week),
            home_team_id=game["home_id"],
            away_team_id=game["away_id"],
            cbs_spread=spread,
            status=game["status"],
            home_score=game["home_score"],
            away_score=game["away_score"],
            game_time=kickoff,
            **values,
        )
        self._games[game_id] = game
        return game_id

    def pick(self, game_id: int, team: str, *names: str) -> None:
        game = self._games[game_id]
        side = ats_side(game)
        team_id = self.teams[team]
        picked_side = "home" if team_id == game["home_id"] else "away"
        correct = None if side in (None, "push") else side == picked_side
        for name in names:
            self.seed.pick(self.user(name), game_id, team_id, is_correct=correct)

    def pool(self, week: int, *names: str, correct: int = 0) -> None:
        """weekly_performance rows - the pool size popular picks go by"""
        for name in names:
            self.seed.performance(self.user(name), self.week(week), correct)


def _recap(d1: FakeD1, week: int) -> dict[str, Any]:
    payload = recap.compute_week_recap(d1, week)
    assert payload is not None
    return payload


def _items(payload: dict[str, Any], item_id: str) -> list[dict[str, Any]]:
    return [i for i in payload["items"] if i["id"] == item_id]


def _item(payload: dict[str, Any], item_id: str) -> dict[str, Any]:
    (item,) = _items(payload, item_id)
    return item


class TestHelpers:
    def test_z_score(self) -> None:
        assert recap._z(0, 0) == 0.0
        assert recap._z(10, 20) == 0.0
        assert recap._z(15, 20) == pytest.approx(2.236, abs=0.001)
        assert recap._z(5, 20) == pytest.approx(-2.236, abs=0.001)

    @pytest.mark.parametrize(
        ("successes", "n", "min_n", "expected"),
        [
            (14, 20, 20, True),  # z 1.79
            (13, 20, 20, False),  # z 1.34 - could be a coin flip
            (6, 20, 20, True),  # a lean either way counts
            (10, 10, 20, False),  # too few, however lopsided
        ],
    )
    def test_stands_out(
        self, successes: int, n: int, min_n: int, expected: bool
    ) -> None:
        assert recap._stands_out(successes, n, min_n) is expected

    @pytest.mark.parametrize(
        ("n", "text"),
        [
            (1, "1st"),
            (2, "2nd"),
            (3, "3rd"),
            (4, "4th"),
            (11, "11th"),
            (12, "12th"),
            (13, "13th"),
            (21, "21st"),
            (22, "22nd"),
            (111, "111th"),
            (112, "112th"),
        ],
    )
    def test_ordinal(self, n: int, text: str) -> None:
        assert recap._ordinal(n) == text

    def test_names_text(self) -> None:
        assert recap._names_text(["a", "b"]) == "a, b"
        assert recap._names_text(["a", "b", "c", "d", "e"]) == "a, b, c and 2 more"
        assert recap._names_text(["a", "b", "c"], limit=2) == "a, b and 1 more"

    def test_fit_trims_at_a_word(self) -> None:
        text = "5-0: " + ", ".join(f"Somebody Longname{i}" for i in range(8))
        short = recap._fit(text)
        assert len(short) <= recap._SHORT_MAX
        assert short.endswith("…")
        assert text.startswith(short[:-1])
        assert not short[:-1].endswith((" ", ","))
        assert recap._fit("short") == "short"

    @pytest.mark.parametrize(
        ("kickoff", "slot"),
        [
            ("2026-09-10T00:20:00Z", "wednesday"),  # a Wednesday night opener
            ("2026-09-11T00:15:00Z", "thursday"),  # 8:15pm ET is Friday in UTC
            ("2026-09-13T13:30:00Z", "sunday_morning"),  # London, 9:30am ET
            ("2026-09-13T17:00:00Z", "sunday_early"),
            ("2026-09-13T20:25:00Z", "sunday_late"),
            ("2026-09-14T00:20:00Z", "sunday_night"),
            ("2026-09-15T00:15:00Z", "monday"),
            ("2026-11-08T18:00:00Z", "sunday_early"),  # 1pm EST, after DST ends
            ("2026-11-08T21:25:00Z", "sunday_late"),  # 4:25pm EST
            ("2026-12-26T01:00:00Z", "friday"),  # Christmas night
        ],
    )
    def test_kickoff_slot(self, kickoff: str, slot: str) -> None:
        assert recap._kickoff_slot({"game_time": kickoff}) == slot

    def test_favorite_and_winner(self) -> None:
        game = {
            "cbs_spread": 3.0,
            "status": "FINAL",
            "home_score": 20,
            "away_score": 17,
        }
        assert recap._favorite_side(game) == "away"
        assert recap._winner_side(game) == "home"
        assert recap._favorite_side({**game, "cbs_spread": 0}) is None
        assert recap._winner_side({**game, "away_score": 20}) is None
        assert recap._winner_side({**game, "status": "IN_PROGRESS"}) is None


class TestPayload:
    def test_no_games_is_none(self, d1: FakeD1, seed: Seed) -> None:
        seed.week(1)
        assert recap.compute_week_recap(d1, 1) is None

    def test_shape(self, d1: FakeD1, seed: Seed) -> None:
        slate = _busy_week(seed)

        payload = _recap(d1, 1)

        assert payload["version"] == recap.SCHEMA_VERSION
        assert (payload["season"], payload["week"]) == (SEASON, 1)
        assert payload["week_complete"] is True
        assert payload["games_final"] == payload["games_total"] == len(slate._games)
        scores = [i["score"] for i in payload["items"]]
        assert scores == sorted(scores, reverse=True)
        ids = [i["id"] for i in payload["items"]]
        assert len(ids) == len(set(ids))
        for item in payload["items"]:
            assert len(item["short"]) <= recap._SHORT_MAX

    def test_later_weeks_are_left_out(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        g1 = slate.game(1, "KC", "BUF", score=(30, 10))
        slate.pick(g1, "KC", "a")
        g2 = slate.game(2, "KC", "NYJ", score=(30, 10))
        slate.pick(g2, "KC", "a")

        payload = _recap(d1, 1)

        assert [row["week"] for row in payload["series"]["pool_accuracy"]] == [1]


def _busy_week(seed: Seed) -> Slate:
    """Five final games, with a 5-0, an 0-5 and one short of five picks"""
    slate = Slate(seed)
    games = [
        slate.game(1, "KC", "BUF", score=(30, 10)),  # KC covers
        slate.game(1, "NYJ", "MIA", score=(30, 10)),  # NYJ
        slate.game(1, "DAL", "PHI", score=(30, 10)),  # DAL
        slate.game(1, "GB", "CHI", score=(30, 10)),  # GB
        slate.game(1, "SF", "SEA", score=(30, 10)),  # SF
    ]
    for game_id, (home, away) in zip(
        games,
        (("KC", "BUF"), ("NYJ", "MIA"), ("DAL", "PHI"), ("GB", "CHI"), ("SF", "SEA")),
    ):
        slate.pick(game_id, home, "perfect")
        slate.pick(game_id, away, "winless")
    for game_id, team in zip(games[:4], ("KC", "NYJ", "DAL", "GB")):
        slate.pick(game_id, team, "four")
    return slate


class TestPoolAccuracy:
    def test_perfect_and_winless(self, d1: FakeD1, seed: Seed) -> None:
        _busy_week(seed)

        payload = _recap(d1, 1)

        accuracy = _item(payload, "pool_accuracy")
        assert (accuracy["data"]["correct"], accuracy["data"]["graded"]) == (9, 14)
        assert "so far" not in accuracy["headline"]
        assert [u["name"] for u in _item(payload, "perfect_week")["data"]["users"]] == [
            "perfect"
        ]
        # "four" went 4-0 on four picks - not a perfect week
        assert [u["name"] for u in _item(payload, "winless_week")["data"]["users"]] == [
            "winless"
        ]

    def test_week_in_progress(self, d1: FakeD1, seed: Seed) -> None:
        slate = _busy_week(seed)
        later = slate.game(1, "LV", "DEN")  # not played yet
        slate.pick(later, "LV", "late")

        payload = _recap(d1, 1)

        assert payload["week_complete"] is False
        assert "so far" in _item(payload, "pool_accuracy")["headline"]

    def test_perfect_needs_all_five_graded(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for i in range(4):
            slate.pick(slate.game(1, f"H{i}", f"A{i}", score=(30, 10)), f"H{i}", "a")
        slate.pick(slate.game(1, "H4", "A4"), "H4", "a")  # 4-0, one to play

        payload = _recap(d1, 1)

        assert _items(payload, "perfect_week") == []

    def test_winless_needs_five_picks(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for i in range(4):
            slate.pick(slate.game(1, f"H{i}", f"A{i}", score=(30, 10)), f"A{i}", "a")

        assert _items(_recap(d1, 1), "winless_week") == []

    def test_nobody_perfect_once_the_week_is_done(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        slate.pick(slate.game(1, "KC", "BUF", score=(30, 10)), "BUF", "a")

        item = _item(_recap(d1, 1), "perfect_week")

        assert item["data"]["users"] == []
        assert item["headline"] == "Nobody went 5-0 this week."

    def test_best_week_of_the_season(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        g1 = slate.game(1, "KC", "BUF", score=(30, 10))
        slate.pick(g1, "BUF", "a")
        g2 = slate.game(2, "KC", "NYJ", score=(30, 10))
        slate.pick(g2, "KC", "a")

        item = _item(_recap(d1, 2), "pool_accuracy")

        assert item["data"]["season_rank_note"] == "best"
        assert item["data"]["prior_accuracy"] == 0.0


class TestSpreadMattered:
    def test_flipped_games_and_burned_picks(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        flipped = slate.game(1, "KC", "BUF", spread=-7.0, score=(24, 20))
        slate.pick(flipped, "KC", "a", "b")  # had the winner, still lost
        slate.pick(flipped, "BUF", "c")
        slate.game(1, "NYJ", "MIA", spread=-3.0, score=(30, 10))
        slate.game(1, "DAL", "PHI", spread=-3.0, score=(20, 17))  # push
        slate.game(1, "GB", "CHI", spread=0.0, score=(20, 17))  # pick'em - skipped

        item = _item(_recap(d1, 1), "spread_mattered")

        data = item["data"]
        assert (data["games"], data["winner_covered"], data["spread_flipped"]) == (
            3,
            1,
            1,
        )
        assert data["pushes"] == 1
        assert data["winner_lost_picks"] == 2
        assert [g["game_id"] for g in data["flipped_games"]] == [flipped]


class TestCrowd:
    def test_popular_picks_use_the_pool_share(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        names = [f"u{i}" for i in range(11)]
        slate.pool(1, *names)  # 0.3 * 11 = 3.3, rounded up to 4 picks
        popular = slate.game(1, "KC", "BUF", score=(30, 10))
        slate.pick(popular, "KC", *names[:4])
        slate.pick(popular, "BUF", names[4])
        crowd_only = slate.game(1, "NYJ", "MIA", score=(10, 30))
        slate.pick(crowd_only, "NYJ", *names[5:8])  # 3 - just short

        payload = _recap(d1, 1)

        week = _item(payload, "popular_picks")
        assert week["data"]["min_picks"] == 4
        assert week["data"]["pool_size"] == 11
        assert (week["data"]["wins"], week["data"]["losses"]) == (1, 0)
        assert [g["game_id"] for g in week["data"]["games"]] == [popular]
        assert week["headline"].startswith("Teams 4+ of you picked went 1-0")
        crowd = _item(payload, "crowd_record")
        assert (crowd["data"]["wins"], crowd["data"]["losses"]) == (1, 1)

    def test_fading_the_crowd(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for week in (1, 2, 3):
            game = slate.game(week, f"H{week}", f"A{week}", score=(10, 30))
            slate.pick(game, f"H{week}", "a", "b")

        item = _item(_recap(d1, 3), "crowd_record:season")

        assert item["headline"].endswith("- fading it would be 3-0.")
        assert (item["data"]["fade_wins"], item["data"]["fade_losses"]) == (3, 0)

    @pytest.mark.xfail(
        strict=True,
        reason="known bug, CLAUDE.local.md recap backlog: season headline uses the "
        "current week's min_picks, None before that week has weekly_performance",
    )
    def test_season_headline_before_the_week_has_a_pool(
        self, d1: FakeD1, seed: Seed
    ) -> None:
        slate = Slate(seed)
        names = [f"u{i}" for i in range(10)]
        for week in (1, 2, 3):
            slate.pool(week, *names)
            game = slate.game(week, f"H{week}", f"A{week}", score=(30, 10))
            slate.pick(game, f"H{week}", *names[:4])
        slate.game(4, "H4", "A4")  # week 4's picks haven't loaded yet

        item = _item(_recap(d1, 4), "popular_picks:season")

        assert "None" not in item["headline"]


class TestChaos:
    def _week(self, slate: Slate, final: int, total: int = 10) -> None:
        # every favorite (home, -3) loses outright
        for i in range(total):
            score = (10, 20) if i < final else None
            slate.game(1, f"H{i}", f"A{i}", score=score)

    def test_needs_eight_final_games(self, d1: FakeD1, seed: Seed) -> None:
        self._week(Slate(seed), final=7)
        payload = _recap(d1, 1)
        assert _items(payload, "chaos_index") == []
        assert payload["series"]["chaos"] == []

    def test_partial_week(self, d1: FakeD1, seed: Seed) -> None:
        self._week(Slate(seed), final=8)

        item = _item(_recap(d1, 1), "chaos_index")

        assert item["data"]["partial"] is True
        assert "so far (8 of 10 games)" in item["headline"]
        # every underdog covered and won - as chaotic as it gets
        assert item["data"]["index"] == 10.0

    def test_complete_week_ranks_against_the_season(
        self, d1: FakeD1, seed: Seed
    ) -> None:
        slate = Slate(seed)
        for i in range(8):
            slate.game(1, f"H{i}", f"A{i}", score=(30, 10))  # all chalk
        for i in range(8):
            slate.game(2, f"H{i}", f"A{i}", score=(10, 30))  # all upsets

        assert _item(_recap(d1, 1), "chaos_index")["data"]["index"] == 0.0
        week2 = _item(_recap(d1, 2), "chaos_index")
        assert week2["data"]["season_rank"] == 1
        assert week2["headline"].endswith("The most chaotic week of the season.")


class TestTwinsAndOppos:
    def test_twins_and_oppos(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for i in range(5):
            game = slate.game(1, f"H{i}", f"A{i}", score=(30, 10))
            slate.pick(game, f"H{i}", "twin1", "twin2")
            slate.pick(game, f"A{i}", "oppo")
            # opposite twin1 on four games, with them on the last
            slate.pick(game, f"H{i}" if i == 4 else f"A{i}", "mixed")
        slate.pick(slate.game(1, "X", "Y", score=(30, 10)), "X", "partial")

        payload = _recap(d1, 1)

        twins = [i for i in payload["items"] if i["kind"] == "twins"]
        assert [[u["name"] for u in t["data"]["users"]] for t in twins] == [
            ["twin1", "twin2"]
        ]
        assert twins[0]["data"]["record"] == {"correct": 5, "graded": 5}
        oppos = [i for i in payload["items"] if i["kind"] == "oppos"]
        pairs = sorted(sorted(u["name"] for u in o["data"]["users"]) for o in oppos)
        # "mixed" shares a side with everyone on at least one game
        assert pairs == [["oppo", "twin1"], ["oppo", "twin2"]]
        assert "won it 5-0" in oppos[0]["headline"]


class TestCoverStreaks:
    def test_active_streaks(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for week in (1, 2, 3):
            slate.game(week, "KC", f"OPP{week}", score=(30, 10))  # KC covers
            slate.game(week, "NYJ", f"FOE{week}", score=(20, 17))  # push each week
        slate.game(4, "BUF", "OPP4", score=(10, 30))  # BUF misses, only once

        payload = _recap(d1, 4)

        streaks = {s["team"]["abbr"]: s for s in payload["cover_streaks"]}
        assert streaks["KC"]["streak_type"] == "cover"
        assert streaks["KC"]["length"] == 3
        assert "NYJ" not in streaks and "BUF" not in streaks
        item = _item(payload, "cover_streak:cover")
        assert item["headline"] == "KC has covered 3 straight."

    def test_a_push_ends_a_streak(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for week in (1, 2, 3):
            slate.game(week, "KC", f"OPP{week}", score=(30, 10))
        slate.game(4, "KC", "OPP4", score=(20, 17))

        assert _recap(d1, 4)["cover_streaks"] == []


class TestMovers:
    def test_biggest_climb(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for name, week1, week2 in (
            ("a", 5, 0),
            ("b", 4, 0),
            ("c", 3, 0),
            ("d", 0, 5),
        ):
            slate.pool(1, name, correct=week1)
            slate.pool(2, name, correct=week2)
        slate.game(2, "KC", "BUF", score=(30, 10))

        payload = _recap(d1, 2)

        climb = _item(payload, "biggest_mover:up")
        (move,) = climb["data"]["moves"]
        assert (move["name"], move["rank_before"], move["rank_after"]) == ("d", 4, 1)
        assert climb["headline"] == "d jumped 3 spots to 1st."
        assert [m["name"] for m in payload["movers"]] == ["d"]

    def test_no_movers_in_week_one(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        slate.pool(1, "a", "b", "c", "d", correct=3)
        slate.game(1, "KC", "BUF", score=(30, 10))

        assert _recap(d1, 1)["movers"] == []


class TestUpset:
    def test_biggest_underdog_to_win(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        small = slate.game(1, "KC", "BUF", spread=-3.0, score=(10, 20))
        slate.pick(small, "BUF", "a")
        big = slate.game(1, "NYJ", "MIA", spread=9.5, score=(24, 21))  # NYJ +9.5
        slate.pick(big, "NYJ", "believer")
        slate.pick(big, "MIA", "b", "c")

        item = _item(_recap(d1, 1), "upset_of_week")

        assert item["data"]["game_id"] == big
        assert item["headline"] == (
            "Upset of the week: NYJ (+9.5) beat MIA outright."
            " 1 of the 3 who picked that game had them: believer."
        )


class TestSplits:
    def test_league_home_road_split(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for i in range(8):
            slate.game(1, f"H{i}", f"A{i}", score=(30, 10))  # home covers
        for i in range(8, 10):
            slate.game(1, f"H{i}", f"A{i}", score=(10, 30))

        item = _item(_recap(d1, 1), "home_road_covers:league")

        assert item["headline"] == "Home teams are 8-2 against the spread this season."
        assert (item["data"]["successes"], item["data"]["n"]) == (8, 10)

    def test_small_or_even_splits_stay_out(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for i in range(9):  # 9-0, but under the 10-game floor
            slate.game(1, f"H{i}", f"A{i}", score=(30, 10))

        payload = _recap(d1, 1)

        assert _items(payload, "home_road_covers:league") == []

    def test_neutral_site_skips_home_road(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for i in range(10):
            slate.game(1, f"H{i}", f"A{i}", score=(30, 10), neutral_site=True)

        payload = _recap(d1, 1)

        assert _items(payload, "home_road_covers:league") == []
        # favorites still count - the spread doesn't care where it's played
        assert _item(payload, "favorite_covers:league")["data"]["n"] == 10

    def test_team_split(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        for week in (1, 2, 3, 4):
            slate.game(week, "KC", f"OPP{week}", score=(30, 10))

        payload = _recap(d1, 4)

        item = _item(payload, "team_split:KC:home")
        assert item["headline"] == "KC are 4-0 against the spread at home this season."

    def test_pool_split(self, d1: FakeD1, seed: Seed) -> None:
        slate = Slate(seed)
        names = [f"u{i}" for i in range(10)]
        for i in range(2):
            game = slate.game(1, f"H{i}", f"A{i}", score=(10, 30))  # dogs cover
            slate.pick(game, f"A{i}", *names)  # the pool took the dogs

        item = _item(_recap(d1, 1), "pool_split:fav:underdog")

        assert (item["data"]["successes"], item["data"]["n"]) == (20, 20)
        assert item["data"]["favorite_pick_share"] == 0.0


class TestWriteRecentWeeks:
    @pytest.fixture(autouse=True)
    def _fakes(self, clients: Clients) -> None:
        """every test here writes through the fake D1/KV"""

    def test_which_weeks_get_refreshed(
        self, clients: Clients, seed: Seed, d1: FakeD1
    ) -> None:
        now = datetime.now(UTC)
        slate = Slate(seed)

        def week(number: int, start: timedelta, end: timedelta, **flags: bool) -> None:
            week_id = slate.week(number)
            d1.query(
                "UPDATE weeks SET start_time = ?, end_time = ?, is_complete = ?,"
                " is_current = ? WHERE week_id = ?",
                [
                    iso(now + start),
                    iso(now + end),
                    flags.get("complete", False),
                    flags.get("current", False),
                    week_id,
                ],
            )
            slate.game(number, f"H{number}", f"A{number}", kickoff=now + start)

        day = timedelta(days=1)
        week(1, -10 * day, -6 * day, complete=True)  # long done
        week(2, -5 * day, -2 * day)  # started, never marked complete
        week(3, -3 * day, -timedelta(hours=2), complete=True)  # MNF just ended
        week(4, 2 * day, 5 * day, current=True)  # CBS already moved on
        week(5, 9 * day, 12 * day)  # hasn't started

        recap.write_recent_weeks_recap()

        assert sorted(clients.kv.values) == [
            f"week:{SEASON}:02:recap",
            f"week:{SEASON}:03:recap",
            f"week:{SEASON}:04:recap",
        ]
        assert clients.kv.values[f"week:{SEASON}:03:recap"]["week"] == 3
