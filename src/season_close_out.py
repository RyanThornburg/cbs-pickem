"""Run once, by hand, after the season is truly over
Usage: uv run python -m src.season_close_out [local|prod]
"""

import json
import logging

from config.config import PERIODS, SEASON, run_cli
from db.clients import get_d1
from db.d1_client import D1Client, Statement
from src.kv_writer import compute_week_leaderboard, write_historical
from src.periods import final_week_number, period_definitions

logger = logging.getLogger(__name__)

_UPSERT_STANDING_SQL = """
INSERT INTO historical_standings
    (season_id, user_id, pool_name, final_rank, final_score,
     first_half_rank, first_half_score, second_half_rank, second_half_score,
     last_place)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(season_id, user_id) DO UPDATE SET
    pool_name = excluded.pool_name,
    final_rank = excluded.final_rank,
    final_score = excluded.final_score,
    first_half_rank = excluded.first_half_rank,
    first_half_score = excluded.first_half_score,
    second_half_rank = excluded.second_half_rank,
    second_half_score = excluded.second_half_score,
    last_place = excluded.last_place
"""

# a re-run replaces the season's period rows, so a period renamed in
# config in between doesn't leave the old key behind
_CLEAR_PERIODS_SQL = "DELETE FROM historical_period_standings WHERE season_id = ?"

_INSERT_PERIOD_SQL = """
INSERT INTO historical_period_standings
    (season_id, user_id, period_key, rank, score, last_place)
VALUES (?, ?, ?, ?, ?, ?)
"""

_SAVE_PERIODS_SQL = "UPDATE seasons SET periods_json = ? WHERE season_id = ?"

_UNFINISHED_WEEKS_SQL = """
SELECT COUNT(*) AS unfinished FROM weeks WHERE season_id = ? AND is_complete = 0
"""


def _season_pool_name(d1: D1Client) -> str | None:
    result = d1.query("SELECT name FROM seasons WHERE season_id = ?", [SEASON])
    return result.results[0]["name"] if result.results else None


def close_out_season() -> None:
    """Close out config.SEASON (the current season) - compute_week_leaderboard()
    is itself hardcoded to config.SEASON, so this can never operate on any
    other season without mislabeling that season's real data."""
    d1 = get_d1()

    final_week = final_week_number(d1)
    if final_week is None:
        logger.warning("No weeks found for season %s - nothing to close out", SEASON)
        return

    # last place eligibility only counts finished weeks, so a week still in
    # play would be skipped rather than judged
    unfinished = d1.query(_UNFINISHED_WEEKS_SQL, [SEASON]).results[0]["unfinished"]
    if unfinished:
        logger.warning(
            "Season %s still has %d unfinished week(s) - not closing out",
            SEASON,
            unfinished,
        )
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
    statements: list[Statement] = [
        (
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
                entry["periods"]["overall"]["in_money_last_place"],
            ],
        )
        for entry in standings
    ]
    statements.append((_CLEAR_PERIODS_SQL, [SEASON]))
    for entry in standings:
        for period in PERIODS:
            if period.key == "overall":  # historical_standings' own columns
                continue
            standing = entry["periods"][period.key]
            statements.append(
                (
                    _INSERT_PERIOD_SQL,
                    [
                        SEASON,
                        entry["user_id"],
                        period.key,
                        standing["place"],
                        standing["score"],
                        standing["in_money_last_place"],
                    ],
                )
            )
    statements.append(
        (_SAVE_PERIODS_SQL, [json.dumps(period_definitions(final_week)), SEASON])
    )
    # one atomic batch, so a failure partway can't leave a partial close-out
    d1.batch(statements)

    logger.info(
        "Closed out season %s (final week %d) - wrote %d historical_standings rows",
        SEASON,
        final_week,
        len(standings),
    )

    write_historical()


def main() -> None:
    close_out_season()


if __name__ == "__main__":
    run_cli(main)
