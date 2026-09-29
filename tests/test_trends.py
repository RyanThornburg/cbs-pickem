"""src/kv_writer/trends.py (week and season trends keys) plus the shared
ats_side() every ATS grade in the KV writer goes through."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from config.config import SEASON
from src.kv_writer import trends
from src.kv_writer.shared import ats_side
from tests.conftest import Clients, FakeD1, Seed

KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)


def _game(
    home_score: int | None,
    away_score: int | None,
    spread: float | None,
    status: str = "FINAL",
    game_id: int = 1,
) -> dict[str, Any]:
    return {
        "game_id": game_id,
        "status": status,
        "home_score": home_score,
        "away_score": away_score,
        "cbs_spread": spread,
        "home_id": 1,
        "home_abbr": "HOM",
        "home_name": "Home",
        "away_id": 2,
        "away_abbr": "AWY",
        "away_name": "Away",
    }


def _picks(team_id: int, count: int, first_user: int = 1) -> list[dict[str, Any]]:
    return [
        {"user_id": u, "name": f"u{u}", "picked_team_id": team_id, "game_id": 1}
        for u in range(first_user, first_user + count)
    ]


class TestAtsSide:
    @pytest.mark.parametrize(
        ("home", "away", "spread", "expected"),
        [
            (24, 20, -3.0, "home"),  # won by 4, laid 3
            (24, 21, -3.0, "push"),
            (23, 21, -3.0, "away"),  # won by 2, didn't cover
            (20, 24, 6.5, "home"),  # lost by 4 getting 6.5
            (17, 24, 6.5, "away"),
            (20, 20, 0.0, "push"),
        ],
    )
    def test_final_games(
        self, home: int, away: int, spread: float, expected: str
    ) -> None:
        assert ats_side(_game(home, away, spread)) == expected

    @pytest.mark.parametrize(
        "game",
        [
            _game(24, 20, -3.0, status="IN_PROGRESS"),
            _game(24, 20, None),
            _game(None, 20, -3.0),
        ],
    )
    def test_undecided(self, game: dict[str, Any]) -> None:
        assert ats_side(game) is None


class TestSpreadBuckets:
    @pytest.mark.parametrize(
        ("spread", "bucket"),
        [
            (0.0, "0-3"),
            (2.5, "0-3"),
            (3.0, "3-7"),
            (7.0, "7-14"),
            (13.5, "7-14"),
            (14.0, "14+"),
            (21.0, "14+"),
        ],
    )
    def test_boundaries(self, spread: float, bucket: str) -> None:
        assert trends._spread_bucket(spread) == bucket

    def test_pick_outcomes(self) -> None:
        # home won by 2 laying 3: right straight up, wrong against the spread
        assert trends._pick_outcomes(_game(23, 21, -3.0), 1) == (True, False)
        assert trends._pick_outcomes(_game(23, 21, -3.0), 2) == (False, True)
        # a push grades as neither against the spread, a tie as neither straight up
        assert trends._pick_outcomes(_game(24, 21, -3.0), 1) == (True, None)
        # a tie getting 3 points still covers
        assert trends._pick_outcomes(_game(20, 20, 3.0), 1) == (None, True)
        assert trends._pick_outcomes(_game(20, 20, 3.0, status="HALFTIME"), 1) == (
            None,
            None,
        )

    def test_bucket_accuracy(self) -> None:
        games = [_game(23, 21, -3.0)]
        picks = {1: _picks(1, 3) + _picks(2, 1, first_user=4)}

        result = trends._spread_bucket_trends(games, picks)

        (bucket,) = result["by_bucket"]
        assert bucket["bucket"] == "3-7"
        assert bucket["overall"] == {
            "straight_up_pick_count": 4,
            "straight_up_accuracy": 0.75,
            "ats_pick_count": 4,
            "ats_accuracy": 0.25,
        }
        assert bucket["home_picks"]["ats_accuracy"] == 0.0
        assert bucket["away_picks"]["ats_accuracy"] == 1.0


class TestWeekHelpers:
    @pytest.mark.parametrize(
        ("home", "away", "side"),
        [(4, 1, "home"), (1, 4, "away"), (3, 1, None), (2, 0, None)],
    )
    def test_one_sided(self, home: int, away: int, side: str | None) -> None:
        # 80% consensus on at least 3 picks
        entry = trends._one_sided_entry(
            _game(None, None, -3.0), _picks(1, home), _picks(2, away, 10)
        )
        assert (entry["consensus_side"] if entry else None) == side

    def test_all_alone_needs_a_real_crowd(self) -> None:
        game = _game(None, None, -3.0, status="SCHEDULED")
        assert trends._all_alone_entries(game, _picks(1, 1), _picks(2, 2, 10)) == []
        (entry,) = trends._all_alone_entries(game, _picks(1, 1), _picks(2, 3, 10))
        assert entry["abbr"] == "HOM"
        assert entry["opposing_count"] == 3
        assert entry["correct"] is None  # not final yet

    @pytest.mark.parametrize(
        ("score", "correct"), [((24, 20), True), ((21, 20), False), ((23, 20), None)]
    )
    def test_all_alone_grading(
        self, score: tuple[int, int], correct: bool | None
    ) -> None:
        game = _game(*score, -3.0)
        (entry,) = trends._all_alone_entries(
            game, _picks(1, 1), _picks(2, 3, 10), week_number=2
        )
        assert entry["correct"] is correct
        assert entry["week_number"] == 2

    def test_movers_skip_noise_and_sort_by_size(self) -> None:
        lookup = {g: _game(None, None, -3.0, game_id=g) for g in (1, 2, 3)}
        consensus = {
            1: {"open": -3.0, "close": -3.5, "book_count": 5},
            2: {"open": -3.0, "close": -6.0, "book_count": 5},
            3: {"open": -3.0, "close": -1.5, "book_count": 4},
        }
        movers = trends._movers_from_consensus(consensus, lookup)
        assert [(m["game_id"], m["movement"]) for m in movers] == [(2, -3.0), (3, 1.5)]


class TestSeasonHelpers:
    def test_trap_team_ranking(self) -> None:
        totals = [
            {"id": 1, "pct_of_all_picks": 0.3},
            {"id": 2, "pct_of_all_picks": 0.3},
            {"id": 3, "pct_of_all_picks": 0.05},
            {"id": 4, "pct_of_all_picks": 0.2},
        ]
        ats = [
            {"id": 1, "cover_pct": 0.25},
            {"id": 2, "cover_pct": 0.75},
            {"id": 3, "cover_pct": 0.0},
            {"id": 4, "cover_pct": None},  # only pushes - can't rank
        ]
        ranked = trends._trap_team_ranking(totals, ats)
        assert [t["id"] for t in ranked] == [1, 2, 3]
        assert ranked[0]["trap_score"] == 0.225

    def test_believers_and_faders_mirror_each_other(self) -> None:
        # team 1 covered: believers right, faders wrong
        games = [_game(24, 20, -3.0)]
        picks = {1: _picks(1, 3) + _picks(2, 1, first_user=4)}
        teams = {1: {"id": 1}, 2: {"id": 2}}

        by_team = {
            t["id"]: t for t in trends._believers_and_faders(games, picks, teams)
        }

        assert by_team[1]["believers"] == {"pick_count": 3, "accuracy": 1.0}
        assert by_team[1]["faders"] == {"pick_count": 1, "accuracy": 0.0}
        assert by_team[2]["believers"] == {"pick_count": 1, "accuracy": 0.0}
        assert by_team[2]["faders"] == {"pick_count": 3, "accuracy": 1.0}


class Week:
    """A few seeded games in one week, with picks by named users"""

    def __init__(self, seed: Seed, number: int = 1, like: Week | None = None) -> None:
        """`like`: another week whose users and teams this one reuses"""
        self.seed = seed
        self.number = number
        self.week_id = seed.week(number)
        seed.d1.query(
            "UPDATE weeks SET is_current = 1 WHERE week_id = ?", [self.week_id]
        )
        self.users: dict[str, int] = like.users if like else {}
        self.teams: dict[str, int] = like.teams if like else {}

    def team(self, abbr: str) -> int:
        if abbr not in self.teams:
            self.teams[abbr] = self.seed.team(abbr)
        return self.teams[abbr]

    def game(
        self,
        home: str,
        away: str,
        spread: float = -3.0,
        score: tuple[int, int] | None = None,
    ) -> int:
        kickoff = KICKOFF + timedelta(weeks=self.number - 1, minutes=len(self.teams))
        if score is None:
            return self.seed.game(
                self.week_id,
                home_team_id=self.team(home),
                away_team_id=self.team(away),
                cbs_spread=spread,
                game_time=kickoff,
                status="SCHEDULED",
            )
        return self.seed.final(
            self.week_id, self.team(home), self.team(away), score, spread, kickoff
        )

    def picks(self, game_id: int, team: str, *names: str) -> None:
        for name in names:
            if name not in self.users:
                self.users[name] = self.seed.user(name)
            self.seed.pick(self.users[name], game_id, self.team(team))


def _odds(d1: FakeD1, game_id: int, book: str, point: float, captured_at: str) -> None:
    d1.query(
        "INSERT INTO odds_snapshots (game_id, source, bookmaker, market, captured_at,"
        " home_point, home_price) VALUES (?, 'the_odds_api', ?, 'spread', ?, ?, -110)",
        [game_id, book, captured_at, point],
    )


class TestWriteWeekTrends:
    @pytest.fixture(autouse=True)
    def _fakes(self, clients: Clients) -> None:
        clients.use(trends)

    def _key(self, clients: Clients, week: int = 1) -> dict[str, Any]:
        return clients.kv.values[f"week:{SEASON}:{week:02d}:trends"]

    def test_popularity_cold_teams_and_one_sided(
        self, clients: Clients, seed: Seed
    ) -> None:
        week = Week(seed)
        kc_buf = week.game("KC", "BUF")
        week.picks(kc_buf, "KC", "a", "b", "c", "d")
        nyj_mia = week.game("NYJ", "MIA")
        week.picks(nyj_mia, "NYJ", "e")
        week.game("DAL", "PHI")  # no picks revealed yet

        trends.write_week_trends(1)
        key = self._key(clients)

        assert [(t["abbr"], t["pick_count"]) for t in key["pick_popularity"]] == [
            ("KC", 4),
            ("NYJ", 1),
        ]
        # an unrevealed game isn't "cold" - only a revealed side nobody took
        assert {t["abbr"] for t in key["cold_teams"]} == {"BUF", "MIA"}
        assert [g["game_id"] for g in key["one_sided_games"]] == [kc_buf]
        assert key["one_sided_games"][0]["consensus_pct"] == 1.0

    def test_lone_geniuses_and_fools(self, clients: Clients, seed: Seed) -> None:
        week = Week(seed)
        covered = week.game("KC", "BUF", spread=-3.0, score=(20, 24))
        week.picks(covered, "KC", "a", "b", "c")
        week.picks(covered, "BUF", "genius")
        missed = week.game("NYJ", "MIA", spread=-3.0, score=(30, 10))
        week.picks(missed, "NYJ", "d", "e", "f", "g")
        week.picks(missed, "MIA", "fool")

        trends.write_week_trends(1)
        key = self._key(clients)

        assert [e["name"] for e in key["lone_geniuses"]] == ["genius"]
        assert [e["name"] for e in key["lone_fools"]] == ["fool"]
        assert len(key["all_alone"]) == 2

    def test_spread_movers_from_odds_snapshots(
        self, clients: Clients, seed: Seed
    ) -> None:
        week = Week(seed)
        moved = week.game("KC", "BUF")
        steady = week.game("NYJ", "MIA")
        for book in ("draftkings", "fanduel"):
            _odds(clients.d1, moved, book, -3.0, "2026-09-08T12:00:00Z")
            _odds(clients.d1, moved, book, -5.5, "2026-09-13T12:00:00Z")
            _odds(clients.d1, steady, book, -3.0, "2026-09-08T12:00:00Z")
            _odds(clients.d1, steady, book, -3.5, "2026-09-13T12:00:00Z")
        # books outside the recognizable list don't count
        _odds(clients.d1, steady, "someoffshorebook", -10.0, "2026-09-13T13:00:00Z")

        trends.write_week_trends(1)
        (mover,) = self._key(clients)["spread_movers"]

        assert mover["game_id"] == moved
        assert (mover["open"], mover["close"], mover["movement"]) == (-3.0, -5.5, -2.5)
        assert mover["book_count"] == 2

    def test_no_games_writes_nothing(self, clients: Clients, seed: Seed) -> None:
        seed.week(1)
        trends.write_week_trends(1)
        assert clients.kv.values == {}

    def test_current_week(self, clients: Clients, seed: Seed) -> None:
        Week(seed, number=3).game("KC", "BUF")
        trends.write_current_week_trends()
        assert set(clients.kv.values) == {f"week:{SEASON}:03:trends"}


class TestWriteSeasonTrends:
    @pytest.fixture(autouse=True)
    def _fakes(self, clients: Clients) -> None:
        clients.use(trends)

    def test_season_key(self, clients: Clients, seed: Seed) -> None:
        week1 = Week(seed, 1)
        g1 = week1.game("KC", "BUF", spread=-3.0, score=(27, 20))  # KC covers
        week1.picks(g1, "KC", "a", "b", "c")
        week1.picks(g1, "BUF", "d")
        g2 = week1.game("NYJ", "MIA", spread=-3.0, score=(20, 17))  # push
        week1.picks(g2, "NYJ", "a")
        week1.game("DAL", "PHI")  # not played yet
        week2 = Week(seed, 2, like=week1)
        g3 = week2.game("BUF", "KC", spread=-1.0, score=(30, 10))  # BUF covers
        week2.picks(g3, "KC", "b")

        trends.write_season_trends()
        key = clients.kv.values[f"season:{SEASON}:trends"]

        totals = {t["abbr"]: t["total_picks"] for t in key["team_pick_totals"]}
        assert totals == {"KC": 4, "BUF": 1, "NYJ": 1}
        assert key["team_pick_totals"][0]["pct_of_all_picks"] == round(4 / 6, 3)
        assert {t["abbr"] for t in key["cold_teams_season"]} == {"MIA", "DAL", "PHI"}

        ats = {t["abbr"]: t for t in key["team_ats_record"]}
        assert (ats["KC"]["covers"], ats["KC"]["losses"]) == (1, 1)
        assert ats["KC"]["cover_pct"] == 0.5
        assert (ats["NYJ"]["pushes"], ats["NYJ"]["cover_pct"]) == (1, None)
        assert (ats["MIA"]["covers"], ats["MIA"]["pushes"]) == (0, 1)
        assert "DAL" not in ats  # no final games yet

        # BUF alone against KC's three in week 1
        (loner,) = key["all_alone_season"]
        assert (loner["name"], loner["week_number"], loner["correct"]) == (
            "d",
            1,
            False,
        )

        # KC: 3 right in week 1, 1 wrong in week 2 - and fading BUF is the
        # same four picks graded the same way
        by_team = {t["abbr"]: t for t in key["team_believers_faders"]}
        assert by_team["KC"]["believers"] == {"pick_count": 4, "accuracy": 0.75}
        assert by_team["BUF"]["faders"] == {"pick_count": 4, "accuracy": 0.75}

    def test_no_games_writes_nothing(self, clients: Clients, seed: Seed) -> None:
        seed.week(1)
        trends.write_season_trends()
        assert clients.kv.values == {}
