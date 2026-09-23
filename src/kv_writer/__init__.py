"""write json from db to cloudflare kv for web ui

Usage: uv run python -m src.kv_writer [local|prod]

Split into one module per KV key (see src/CLAUDE.md's KV writer section) -
this file re-exports every public write_*/compute_week_leaderboard name so
existing `from src.kv_writer import ...` call sites (orchestration.py,
season_close_out.py) don't need to change. main()/the CLI entry point live
in __main__.py instead of here - `python -m src.kv_writer` runs a package's
__main__.py, never __init__.py, regardless of what an `if __name__ ==
"__main__":` block here might say."""

from src.kv_writer.admin import write_admin_status
from src.kv_writer.games import (
    write_current_week_games,
    write_incomplete_weeks_games,
    write_week_games,
)
from src.kv_writer.historical import write_historical
from src.kv_writer.leaderboard import (
    compute_week_leaderboard,
    write_current_week_leaderboard,
    write_week_leaderboard,
)
from src.kv_writer.odds import write_current_week_odds, write_week_odds
from src.kv_writer.shared import write_meta_current
from src.kv_writer.trends import (
    write_current_week_trends,
    write_season_trends,
    write_week_trends,
)
from src.kv_writer.user_profiles import write_user_profiles

__all__ = [
    "compute_week_leaderboard",
    "write_admin_status",
    "write_current_week_games",
    "write_current_week_leaderboard",
    "write_current_week_odds",
    "write_current_week_trends",
    "write_historical",
    "write_incomplete_weeks_games",
    "write_meta_current",
    "write_season_trends",
    "write_user_profiles",
    "write_week_games",
    "write_week_leaderboard",
    "write_week_odds",
    "write_week_trends",
]
