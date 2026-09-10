"""Load the current NFL season into the seasons table.

Usage: uv run python -m src.loaders.season_loader [local|prod]

Run at the start of a new season (or on any refresh) to upsert the season
Sports IO currently flags as `current` `season_id`. Any other season row's `is_active`
is cleared first so at most one season is ever active at a time.
"""

import logging
import sys
from typing import Any

from api.sports_io_client import get_current_season
from config.config import configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.loaders.loader_helper import sql_batch_call

logger = logging.getLogger(__name__)

_CLEAR_ACTIVE_SQL = "UPDATE seasons SET is_active = FALSE WHERE season_id != ?"

_UPSERT_SQL = """
INSERT INTO seasons (season_id, name, start_date, end_date, is_active)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(season_id) DO UPDATE SET
    name = excluded.name,
    start_date = excluded.start_date,
    end_date = excluded.end_date,
    is_active = excluded.is_active
"""


def main(env: str = "local") -> None:
    """load current season"""
    if not load_env(env):
        sys.exit(1)

    client = D1Client(**get_d1_config())
    season = get_current_season()
    if season is None:
        logger.warning("Sports IO has no season flagged current! Nothing to load")
        return

    statements: list[Any] = [
        (_CLEAR_ACTIVE_SQL, [season.year]),
        (
            _UPSERT_SQL,
            [
                season.year,
                f"{season.year} Season",
                season.start,
                season.end,
                season.current,
            ],
        ),
    ]

    logger.info("Upserting season %d into D1 (%s)", season.year, env)
    sql_batch_call(statements, client)

    logger.info("Season load complete for %s environment!", env)


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
