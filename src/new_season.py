"""
Create new season and make initial calls needed

Usage: uv run python -m src.new_season [local|prod]
"""

import logging
import sys

from config.config import configure_logging
from src.loaders import cbs_loader, season_loader, teams_loader

logger = logging.getLogger(__name__)


def main(env: str = "local") -> None:
    season_loader.main(env)  # load season from sports io
    teams_loader.main(env)  # load teams from sports io
    cbs_loader.load_cbs_users(env)  # load users for this season
    cbs_loader.map_cbs_to_sports_io(env)  # add cbs ids to team id data


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
