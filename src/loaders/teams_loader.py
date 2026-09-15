"""Load Sports IO team profiles into the teams table

Usage: uv run python -m src.loaders.teams_loader [local|prod]

- sports io team model does not have conference/division, read from standings
- No CBS id is set during load, that needs to happen when CBS data is loaded
- using sports_io_team_id for upserts since most data is linked there

Upserts are using sports_io_team_id
"""

import logging
import sys
from typing import Any

from api.sports_io_client import get_standings, get_teams
from api.sports_io_models import Standing
from config.config import SEASON, configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.loaders.loader_helper import sql_batch_call

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO teams (
    name, season, city, abbreviation, established, logo,
    conference, division, wins, losses, ties, sports_io_team_id
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ties = excluded.ties
"""


def _standings_by_team_id(standings: list[Standing]) -> dict[int, Standing]:
    return {s.team.id: s for s in standings}


def main() -> None:
    """load team data"""
    client = D1Client(**get_d1_config())
    teams = get_teams()
    standings_by_team_id = _standings_by_team_id(get_standings())

    statements: list[tuple[str, list[Any] | None]] = []
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


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    main()
