"""Load Sports IO team profiles into the teams table

Usage: uv run python -m src.loaders.teams_loader [local|prod]

load_standings() refreshes only the standings columns (one Sports IO call),
for after games finish - see orchestration's _run_standings_refresh().

- sports io team model does not have conference/division, read from standings
- No CBS id is set during load, that needs to happen when CBS data is loaded
- using sports_io_team_id for upserts since most data is linked there

Upserts are using sports_io_team_id
"""

import logging

from api.sports_io_client import get_standings, get_teams
from api.sports_io_models import Standing
from config.config import SEASON, run_cli
from db.clients import get_d1
from db.d1_client import Statement
from src.loaders.loader_helper import sql_batch_call

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO teams (
    name, season, city, abbreviation, established, logo,
    conference, division, wins, losses, ties, division_rank, sports_io_team_id
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(sports_io_team_id) DO UPDATE SET
    name = excluded.name,
    season = excluded.season,
    city = excluded.city,
    abbreviation = excluded.abbreviation,
    established = excluded.established,
    logo = excluded.logo,
    conference = excluded.conference,
    division = excluded.division,
    wins = excluded.wins,
    losses = excluded.losses,
    ties = excluded.ties,
    division_rank = excluded.division_rank
"""

_UPDATE_STANDINGS_SQL = """
UPDATE teams SET conference = ?, division = ?, wins = ?, losses = ?, ties = ?,
    division_rank = ?
WHERE sports_io_team_id = ?
"""


def _standings_by_team_id(standings: list[Standing]) -> dict[int, Standing]:
    return {s.team.id: s for s in standings}


def load_teams() -> None:
    """load team data"""
    client = get_d1()
    teams = get_teams()
    standings_by_team_id = _standings_by_team_id(get_standings())

    statements: list[Statement] = []
    for team in teams:
        if not team.code:
            logger.warning(
                "Skipping team %r (sports_io_team_id=%d): no abbreviation",
                team.name,
                team.id,
            )
            continue
        standing = standings_by_team_id.get(team.id)
        conference = standing.conference if standing else None
        division = standing.division if standing else None
        wins = standing.won if standing else None
        losses = standing.lost if standing else None
        ties = standing.ties if standing else None
        division_rank = standing.position if standing else None
        statements.append(
            (
                _UPSERT_SQL,
                [
                    team.name,
                    SEASON,
                    team.city,
                    team.code,
                    team.established,
                    team.logo,
                    conference,
                    division,
                    wins,
                    losses,
                    ties,
                    division_rank,
                    team.id,
                ],
            )
        )

    if not statements:
        logger.warning("No teams to load")
        return

    logger.info("Upserting %d teams into D1", len(statements))
    sql_batch_call(statements, client)

    logger.info("Teams load complete")


def load_standings() -> None:
    """Standings columns only - teams already exist from load_teams()"""
    statements: list[Statement] = [
        (
            _UPDATE_STANDINGS_SQL,
            [
                standing.conference,
                standing.division,
                standing.won,
                standing.lost,
                standing.ties,
                standing.position,
                standing.team.id,
            ],
        )
        for standing in get_standings()
    ]
    if not statements:
        logger.warning("No standings to load")
        return
    sql_batch_call(statements, get_d1())
    logger.info("Standings updated for %d teams", len(statements))


def main() -> None:
    load_teams()


if __name__ == "__main__":
    run_cli(main)
