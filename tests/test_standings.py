"""src/kv_writer/standings.py - NFL standings from our own FINAL games,
with Sports IO's division_rank only breaking ties."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from config.config import SEASON
from src.kv_writer import standings
from tests.conftest import Clients, Seed

KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
AFC = "American Football Conference"
NFC = "National Football Conference"


class Division:
    """four teams per division, teams named by abbreviation"""

    def __init__(self, seed: Seed) -> None:
        self.seed = seed
        self.week_id = seed.week(1)
        self.ids: dict[str, int] = {}
        self.kickoff = KICKOFF

    def teams(self, conference: str, division: str, *abbrs: str) -> None:
        for rank, abbr in enumerate(abbrs, start=1):
            self.ids[abbr] = self.seed.team(
                abbr, conference=conference, division=division, division_rank=rank
            )

    def final(
        self, home: str, away: str, score: tuple[int, int], spread: float = -2.5
    ) -> None:
        self.kickoff += timedelta(hours=1)
        self.seed.final(
            self.week_id, self.ids[home], self.ids[away], score, spread, self.kickoff
        )


@pytest.fixture
def league(seed: Seed) -> Division:
    league = Division(seed)
    league.teams(AFC, "AFC East", "BUF", "MIA", "NE", "NYJ")
    league.teams(NFC, "NFC East", "DAL", "NYG", "PHI", "WAS")
    return league


def _teams(conferences: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        team["team"]["abbr"]: team
        for conference in conferences
        for division in conference["divisions"]
        for team in division["teams"]
    }


def _order(conferences: list[dict[str, Any]], division: str) -> list[str]:
    (found,) = [d for c in conferences for d in c["divisions"] if d["name"] == division]
    return [t["team"]["abbr"] for t in found["teams"]]


class TestStandings:
    def test_shape(self, league: Division, clients: Clients) -> None:
        conferences = standings.compute_standings()

        assert [(c["name"], c["abbr"]) for c in conferences] == [
            (AFC, "AFC"),
            (NFC, "NFC"),
        ]
        assert [d["name"] for d in conferences[0]["divisions"]] == ["AFC East"]
        # before any game, Sports IO's order stands
        assert _order(conferences, "AFC East") == ["BUF", "MIA", "NE", "NYJ"]
        buf = _teams(conferences)["BUF"]
        assert buf["rank"] == 1
        assert buf["win_pct"] is None
        assert buf["streak"] is None
        assert buf["ats"] == {"covers": 0, "losses": 0, "cover_pct": None}

    def test_records(self, league: Division, clients: Clients) -> None:
        league.final("NYJ", "BUF", (24, 20))  # NYJ win, covers -2.5
        league.final("NYJ", "DAL", (21, 20), spread=-3.5)  # win, no cover
        league.final("WAS", "NYJ", (17, 17), spread=1.5)  # tie, NYJ fails -1.5

        nyj = _teams(standings.compute_standings())["NYJ"]

        assert (nyj["wins"], nyj["losses"], nyj["ties"]) == (2, 0, 1)
        assert nyj["win_pct"] == 0.833
        assert (nyj["points_for"], nyj["points_against"], nyj["point_diff"]) == (
            62,
            57,
            5,
        )
        assert nyj["home"] == {"wins": 2, "losses": 0, "ties": 0}
        assert nyj["road"] == {"wins": 0, "losses": 0, "ties": 1}
        assert nyj["division_record"] == {"wins": 1, "losses": 0, "ties": 0}
        assert nyj["conference_record"] == {"wins": 1, "losses": 0, "ties": 0}
        assert nyj["streak"] == "T1"
        assert nyj["ats"] == {"covers": 1, "losses": 2, "cover_pct": 0.333}

    def test_streak(self, league: Division, clients: Clients) -> None:
        league.final("BUF", "MIA", (10, 20))
        league.final("BUF", "NE", (30, 20))
        league.final("BUF", "NYJ", (30, 20))

        assert _teams(standings.compute_standings())["BUF"]["streak"] == "W2"

    def test_win_pct_orders_then_division_rank_breaks_ties(
        self, league: Division, clients: Clients
    ) -> None:
        league.final("NYJ", "BUF", (24, 20))  # NYJ 1-0, BUF 0-1
        league.final("NE", "DAL", (24, 20))  # NE 1-0
        league.final("MIA", "PHI", (10, 20))  # MIA 0-1

        conferences = standings.compute_standings()

        # NE and NYJ tie at 1-0 - Sports IO had NE ahead; same for BUF/MIA
        assert _order(conferences, "AFC East") == ["NE", "NYJ", "BUF", "MIA"]
        assert [t["rank"] for t in conferences[0]["divisions"][0]["teams"]] == [
            1,
            2,
            3,
            4,
        ]

    def test_only_this_seasons_final_games(
        self, league: Division, seed: Seed, clients: Clients
    ) -> None:
        last_season = seed.week(18, season_id=SEASON - 1)
        seed.final(
            last_season, league.ids["BUF"], league.ids["MIA"], (30, 0), -3, KICKOFF
        )
        seed.game(
            league.week_id,
            home_team_id=league.ids["BUF"],
            away_team_id=league.ids["NE"],
            home_score=7,
            away_score=0,
            status="IN_PROGRESS",
        )

        buf = _teams(standings.compute_standings())["BUF"]

        assert (buf["wins"], buf["losses"]) == (0, 0)

    def test_write(self, league: Division, clients: Clients) -> None:
        standings.write_season_standings()

        value = clients.kv.values[f"season:{SEASON}:standings"]
        assert value["season"] == SEASON
        assert len(value["conferences"]) == 2

    def test_no_teams_no_key(self, clients: Clients) -> None:
        standings.write_season_standings()
        assert clients.kv.values == {}
