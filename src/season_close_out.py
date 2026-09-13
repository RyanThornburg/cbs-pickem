"""Run once, by hand, after the season is truly over
Usage: uv run python -m src.season_close_out [local|prod]
"""

import logging
import sys

from config.config import SEASON, configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.kv_writer import compute_week_leaderboard, write_historical

logger = logging.getLogger(__name__)

_UPSERT_STANDING_SQL = """
INSERT INTO historical_standings
    (season_id, user_id, pool_name, final_rank, final_score,
     first_half_rank, first_half_score, second_half_rank, second_half_score)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(season_id, user_id) DO UPDATE SET
    pool_name = excluded.pool_name,
    final_rank = excluded.final_rank,
    final_score = excluded.final_score,
    first_half_rank = excluded.first_half_rank,
    first_half_score = excluded.first_half_score,
    second_half_rank = excluded.second_half_rank,
    second_half_score = excluded.second_half_score
"""


def _final_week_number(d1: D1Client) -> int | None:
    result = d1.query(
        "SELECT MAX(week_number) AS week_number FROM weeks WHERE season_id = ?",
        [SEASON],
    )
    week_number = result.results[0]["week_number"] if result.results else None
    return week_number


def _season_pool_name(d1: D1Client) -> str | None:
    result = d1.query("SELECT name FROM seasons WHERE season_id = ?", [SEASON])
    return result.results[0]["name"] if result.results else None


def close_out_season(env: str = "local") -> None:
    """Close out config.SEASON (the current season) - compute_week_leaderboard()
    is itself hardcoded to config.SEASON, so this can never operate on any
    other season without mislabeling that season's real data."""
    if not load_env(env):
        sys.exit(1)

    d1 = D1Client(**get_d1_config())

    final_week = _final_week_number(d1)
    if final_week is None:
        logger.warning("No weeks found for season %s - nothing to close out", SEASON)
        return

    standings = compute_week_leaderboard(d1, final_week)
    if standings is None:
        logger.warning(
            "No weekly_performance for season %s week %s - nothing to close out",
            SEASON,
            final_week,
        )
        return

    pool_name = _season_pool_name(d1)
    for entry in standings:
        d1.query(
            _UPSERT_STANDING_SQL,
            [
                SEASON,
                entry["user_id"],
                pool_name,
                entry["place"],
                entry["cumulative_score"],
                entry["first_half_place"],
                entry["first_half_score"],
                entry["second_half_place"],
                entry["second_half_score"],
            ],
        )

    logger.info(
        "Closed out season %s (final week %d) - wrote %d historical_standings rows (%s)",
        SEASON,
        final_week,
        len(standings),
        env,
    )

    write_historical(env)


def main(env: str = "local") -> None:
    close_out_season(env)


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
