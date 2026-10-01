"""CLI entry point - see __init__.py. `python -m src.kv_writer [local|prod]`
runs this file, not __init__.py."""

from config.config import run_cli
from src.kv_writer import (
    write_admin_status,
    write_current_week_game_details,
    write_current_week_games,
    write_current_week_leaderboard,
    write_current_week_odds,
    write_current_week_recap,
    write_current_week_trends,
    write_historical,
    write_meta_current,
    write_season_standings,
    write_season_trends,
    write_user_profiles,
)


def main() -> None:
    "write data to kv"
    write_meta_current()
    write_current_week_games()
    write_current_week_game_details()
    write_current_week_leaderboard()
    write_current_week_odds()
    write_current_week_trends()
    write_season_trends()
    write_season_standings()
    write_current_week_recap()
    write_historical()
    write_user_profiles()
    write_admin_status()


if __name__ == "__main__":
    run_cli(main)
