"""
Create new season and make initial calls needed

Usage: uv run python -m src.new_season [local|prod]
"""

import logging

from config.config import run_cli
from src.kv_writer import write_admin_users
from src.loaders import cbs_loader, season_loader, teams_loader

logger = logging.getLogger(__name__)


def main() -> None:
    season_loader.load_season()  # load season from sports io
    teams_loader.load_teams()  # load teams from sports io
    cbs_loader.load_cbs_users()  # load users for this season
    write_admin_users()  # their emails, for the admin page
    cbs_loader.map_cbs_to_sports_io()  # add cbs ids to team id data


if __name__ == "__main__":
    run_cli(main)
