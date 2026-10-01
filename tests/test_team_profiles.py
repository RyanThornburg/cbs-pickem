"""src/kv_writer/team_profiles.py - one key per team: ATS splits, the
pool's record on them, and their game log."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from config.config import SEASON
from src.kv_writer import team_profiles
from tests.conftest import Clients, FakeD1, Seed

KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)


class League:
    def __init__(self, seed: Seed) -> None:
        self.seed = seed
        self.ids = {
            abbr: seed.team(abbr, conference="AFC", division="AFC East")
            for abbr in ("BUF", "MIA", "NE", "NYJ")
        }
        self.users = {name: seed.user(name) for name in ("amy", "bob", "cal")}
        self.week = 0

    def game(
        self,
        home: str,
        away: str,
        spread: float | None = -2.5,
        score: tuple[int, int] | None = None,
    ) -> int:
        self.week += 1
        week_id = self.seed.week(self.week)
        kickoff = KICKOFF + timedelta(weeks=self.week)
        if score is None:
            return self.seed.game(
                week_id,
                home_team_id=self.ids[home],
                away_team_id=self.ids[away],
                cbs_spread=spread,
                game_time=kickoff,
                status="SCHEDULED",
            )
        return self.seed.final(
            week_id, self.ids[home], self.ids[away], score, spread, kickoff
        )

    def pick(self, user: str, game_id: int, team: str, correct: bool | None) -> None:
        self.seed.pick(self.users[user], game_id, self.ids[team], is_correct=correct)


@pytest.fixture
def league(seed: Seed) -> League:
    return League(seed)


def _profile(d1: FakeD1, league: League, abbr: str) -> dict[str, Any]:
    team_id = league.ids[abbr]
    return team_profiles.compute_team_profiles(d1, {team_id})[team_id]


class TestTeamProfile:
    def test_ats_splits_and_record(self, league: League, d1: FakeD1) -> None:
        league.game("BUF", "MIA", -2.5, (24, 20))  # home fav, covers
        league.game("NE", "BUF", -3.5, (20, 17))  # road dog, covers
        league.game("BUF", "NYJ", 1.5, (10, 20))  # home dog, doesn't
        league.game("BUF", "NE", -1.5, None)  # not played yet

        buf = _profile(d1, league, "BUF")

        assert buf["team"] == {
            "id": league.ids["BUF"],
            "abbr": "BUF",
            "name": "Buf",
            "conference": "AFC",
            "division": "AFC East",
        }
        assert buf["record"] == {"wins": 1, "losses": 2, "ties": 0}
        assert buf["ats"]["overall"] == {"covers": 2, "losses": 1, "cover_pct": 0.667}
        assert buf["ats"]["home"] == {"covers": 1, "losses": 1, "cover_pct": 0.5}
        assert buf["ats"]["away"] == {"covers": 1, "losses": 0, "cover_pct": 1.0}
        assert buf["ats"]["favorite"] == {"covers": 1, "losses": 0, "cover_pct": 1.0}
        assert buf["ats"]["underdog"] == {"covers": 1, "losses": 1, "cover_pct": 0.5}

    def test_game_log(self, league: League, d1: FakeD1) -> None:
        played = league.game("NE", "BUF", -3.5, (20, 17))
        upcoming = league.game("BUF", "NYJ", None)
        league.pick("amy", played, "BUF", True)
        league.pick("bob", played, "NE", False)
        league.pick("cal", played, "NE", False)

        games = _profile(d1, league, "BUF")["games"]

        assert [g["game_id"] for g in games] == [played, upcoming]
        assert games[0] | {"game_time": None} == {
            "game_id": played,
            "week_number": 1,
            "game_time": None,
            "side": "away",
            "opponent": {"id": league.ids["NE"], "abbr": "NE", "name": "Ne"},
            "line": 3.5,  # the away team's side of NE -3.5
            "status": "FINAL",
            "score": 17,
            "opponent_score": 20,
            "result": "L",
            "covered": True,
            "pool_picked": 1,
            "pool_against": 2,
        }
        assert games[1]["line"] is None
        assert games[1]["result"] is None
        assert games[1]["covered"] is None
        assert games[1]["score"] is None

    def test_pool_record_and_who_picks_them(self, league: League, d1: FakeD1) -> None:
        first = league.game("BUF", "MIA", -2.5, (24, 20))
        second = league.game("NYJ", "BUF", -1.5, (24, 20))
        third = league.game("BUF", "NE", -1.5, None)
        league.pick("amy", first, "BUF", True)
        league.pick("amy", second, "BUF", False)
        league.pick("bob", first, "BUF", True)
        league.pick("cal", first, "MIA", False)
        league.pick("cal", second, "NYJ", True)
        league.pick("bob", third, "NE", None)  # revealed, not graded

        pool = _profile(d1, league, "BUF")["pool"]

        assert pool["picked"] == {"picks": 3, "wins": 2, "losses": 1, "win_pct": 0.667}
        assert pool["against"] == {"picks": 3, "wins": 1, "losses": 1, "win_pct": 0.5}
        assert [(u["name"], u["picks"], u["wins"]) for u in pool["believers"]] == [
            ("amy", 2, 1),
            ("bob", 1, 1),
        ]
        assert [(u["name"], u["picks"], u["wins"]) for u in pool["faders"]] == [
            ("cal", 2, 1),
            ("bob", 1, 0),
        ]
        assert pool["faders"][1]["win_pct"] is None

    def test_only_this_season(self, league: League, seed: Seed, d1: FakeD1) -> None:
        last_season = seed.week(18, season_id=SEASON - 1)
        seed.final(
            last_season, league.ids["BUF"], league.ids["MIA"], (30, 0), -3, KICKOFF
        )
        assert _profile(d1, league, "BUF")["games"] == []

    def test_every_team_or_just_some(self, league: League, d1: FakeD1) -> None:
        assert len(team_profiles.compute_team_profiles(d1)) == 4
        assert set(
            team_profiles.compute_team_profiles(d1, {league.ids["NE"], 999})
        ) == {league.ids["NE"]}


class TestWrite:
    def test_writes_one_key_per_team(self, league: League, clients: Clients) -> None:
        team_profiles.write_team_profiles({league.ids["BUF"]})

        assert list(clients.kv.values) == [f"team:{SEASON}:{league.ids['BUF']}"]
        assert clients.kv.values[f"team:{SEASON}:{league.ids['BUF']}"]["updated_at"]

    def test_no_teams_no_keys(self, clients: Clients) -> None:
        team_profiles.write_team_profiles()
        assert clients.kv.values == {}


class TestFingerprints:
    def test_changes_with_what_the_key_reads(self, league: League, d1: FakeD1) -> None:
        game = league.game("BUF", "MIA", -2.5)

        def fingerprint() -> str:
            return team_profiles.game_fingerprints(d1)[str(game)][0]

        before = fingerprint()
        assert team_profiles.game_fingerprints(d1)[str(game)][1:] == (
            league.ids["BUF"],
            league.ids["MIA"],
        )
        d1.query(
            "UPDATE games SET status = 'IN_PROGRESS', home_score = 3 WHERE game_id = ?",
            [game],
        )
        live = fingerprint()
        assert live != before
        d1.query("UPDATE games SET home_score = 10 WHERE game_id = ?", [game])
        assert fingerprint() == live  # a live score doesn't count
        league.pick("amy", game, "BUF", None)
        revealed = fingerprint()
        assert revealed != live
        d1.query("UPDATE user_picks SET is_correct = 1")
        assert fingerprint() != revealed
