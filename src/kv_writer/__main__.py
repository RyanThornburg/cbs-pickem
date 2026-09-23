"""CLI entry point - see __init__.py. `python -m src.kv_writer [local|prod]`
runs this file, not __init__.py."""

import sys

from config.config import configure_logging, load_env
from src.kv_writer import (
    write_admin_status,
    write_current_week_games,
    write_current_week_leaderboard,
    write_current_week_odds,
    write_current_week_trends,
    write_historical,
    write_meta_current,
    write_season_trends,
    write_user_profiles,
)


def main() -> None:
    "write data to kv"
    write_meta_current()
    write_current_week_games()
    write_current_week_leaderboard()
    write_current_week_odds()
    write_current_week_trends()
    write_season_trends()
    write_historical()
    write_user_profiles()
    write_admin_status()


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    main()
