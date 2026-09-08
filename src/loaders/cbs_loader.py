"""Load CBS data

Usage: uv run python -m src.loaders.cbs_loader [local|prod]

"""

import logging
import sys
from typing import Any

from api.cbs_client import ABBREV_CORRECTIONS, get_cbs_pool_teams, get_cbs_users
from api.cbs_models import Member
from config.config import configure_logging, get_d1_config, load_env
from db.d1_client import D1Client, D1Error

logger = logging.getLogger(__name__)

_UPSERT_USERS_SQL = """
INSERT INTO users (name, email, cbs_id, is_active)
VALUES (?, ?, ?, ?)
ON CONFLICT(cbs_id) DO UPDATE SET
    name = excluded.name,
    email = excluded.email,
    cbs_id = excluded.cbs_id,
    is_active = excluded.is_active
"""

_UPDATE_CBS_TEAM_SQL = """
UPDATE teams SET
    cbs_team_id = ?,
    medium_name = ?,
    nick_name = ?,
    color_primary_hex = ?,
    color_secondary_hex = ?
WHERE abbreviation = ?
"""


def _sql_batch_call(statements: list[tuple[str, list[Any] | None]]):
    client = D1Client(**get_d1_config())
    try:
        client.batch(statements)
    except D1Error:
        logger.exception("Loading data failed")
        sys.exit(1)


def load_cbs_users(env: str = "local") -> None:
    if not load_env(env):
        sys.exit(1)

    users: list[Member] = get_cbs_users()

    statements: list[tuple[str, list[Any] | None]] = [
        (_UPSERT_USERS_SQL, [user.name, user.email, user.id, True]) for user in users
    ]

    if not statements:
        logger.warning("No Users to load")
        return
    _sql_batch_call(statements)


def map_cbs_to_sports_io(env: str = "local"):
    """
    Match CBS teams to existing team row from Sports IO
    Match on abbreviation and add CBS-only fields.
    """
    if not load_env(env):
        sys.exit(1)

    cbs_teams = get_cbs_pool_teams()
    statements: list[tuple[str, list[Any] | None]] = [
        (
            _UPDATE_CBS_TEAM_SQL,
            [
                team.cbs_team_id,
                team.medium_name,
                team.nick_name,
                team.color_primary_hex,
                team.color_secondary_hex,
                ABBREV_CORRECTIONS.get(team.abbrev, team.abbrev),
            ],
        )
        for team in cbs_teams
    ]

    if not statements:
        logger.warning("No CBS teams to map")
        return

    _sql_batch_call(statements)
    logger.info("Mapped %d CBS teams onto teams table (%s)", len(statements), env)


def load_cbs_games():
    pass


def load_cbs_user_picks():
    pass


def main(env: str = "local") -> None:
    if not load_env(env):
        sys.exit(1)

    load_cbs_user_picks()
    load_cbs_games()


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
