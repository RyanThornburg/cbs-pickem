"""meta:historical - see src/CLAUDE.md's KV writer section.

career_record_by_user() is exported (no leading underscore) because
user_profiles.py's write_user_profiles() needs the same per-user career
record for each user's own profile key."""

import logging
from typing import Any

from config.config import get_d1_config, get_kv_config
from db.d1_client import D1Client
from db.kv_client import KVClient

logger = logging.getLogger(__name__)

# historical season records
_HISTORICAL_SQL = """
SELECT hs.season_id, s.name AS pool_name, s.historical_data_incomplete,
    u.user_id, u.name, u.is_active, hs.final_rank, hs.final_score,
    hs.first_half_rank, hs.first_half_score,
    hs.second_half_rank, hs.second_half_score
FROM historical_standings hs
JOIN users u ON u.user_id = hs.user_id
JOIN seasons s ON s.season_id = hs.season_id
ORDER BY hs.season_id, hs.final_rank
"""


def career_record_by_user(d1: D1Client) -> dict[int, dict[str, Any]]:
    """Per-user career record from historical_standings (prior, closed
    seasons only - the current in-progress season never has a row here
    until season_close_out.py runs at year-end). Shared by write_historical()
    (meta:historical's career list) and src/user_stats.py's
    compute_user_profiles() (each user's own profile key)."""
    career: dict[int, dict[str, Any]] = {}
    for row in d1.query(_HISTORICAL_SQL).results:
        record = career.setdefault(
            row["user_id"],
            {
                "user_id": row["user_id"],
                "name": row["name"],
                "is_active": bool(row["is_active"]),
                "appearances": [],
                "titles": 0,
                "best_finish": None,
                "best_finish_years": [],
                "season_history": [],
            },
        )
        record["appearances"].append(row["season_id"])
        if row["final_rank"] == 1:
            record["titles"] += 1
        if record["best_finish"] is None or row["final_rank"] < record["best_finish"]:
            record["best_finish"] = row["final_rank"]
            record["best_finish_years"] = [row["season_id"]]
        elif row["final_rank"] == record["best_finish"]:
            record["best_finish_years"].append(row["season_id"])
        # rows already come back ordered by season_id (query's own ORDER BY)
        record["season_history"].append(
            {
                "season": row["season_id"],
                "incomplete": bool(row["historical_data_incomplete"]),
                "rank": row["final_rank"],
                "score": row["final_score"],
                "first_half_rank": row["first_half_rank"],
                "first_half_score": row["first_half_score"],
                "second_half_rank": row["second_half_rank"],
                "second_half_score": row["second_half_score"],
            }
        )
    return career


def write_historical() -> None:
    """Write meta:historical - past champions and each user's all-time
    record, from historical_standings (backfilled once from the pre-2026
    archive, and going forward one row per user per season at close-out).
    Static/manual cadence - nothing changes here until a season closes."""
    d1 = D1Client(**get_d1_config())
    rows = d1.query(_HISTORICAL_SQL).results
    if not rows:
        logger.warning(
            "No historical_standings rows found - not writing meta:historical"
        )
        return

    years: dict[str, dict[str, Any]] = {}
    champions_by_season: dict[int, dict[str, Any]] = {}
    first_half_champions_by_season: dict[int, dict[str, Any]] = {}
    second_half_champions_by_season: dict[int, dict[str, Any]] = {}
    career = career_record_by_user(d1)

    for row in rows:
        season_key = str(row["season_id"])
        year = years.setdefault(
            season_key,
            {
                "pool_name": row["pool_name"],
                "incomplete": bool(row["historical_data_incomplete"]),
                "standings": [],
            },
        )
        year["standings"].append(
            {
                "user_id": row["user_id"],
                "name": row["name"],
                "rank": row["final_rank"],
                "score": row["final_score"],
                "first_half_rank": row["first_half_rank"],
                "first_half_score": row["first_half_score"],
                "second_half_rank": row["second_half_rank"],
                "second_half_score": row["second_half_score"],
            }
        )

        champion = champions_by_season.setdefault(
            row["season_id"],
            {
                "year": row["season_id"],
                "incomplete": bool(row["historical_data_incomplete"]),
                "names": ["??? unknown/missing user"]
                if row["historical_data_incomplete"]
                else [],
                "score": None,
            },
        )
        if not champion["incomplete"] and row["final_rank"] == 1:
            champion["names"].append(row["name"])
            champion["score"] = row["final_score"]

        # I didn't track these or have the data
        # season_close_out.py starts writing these going forward.
        if row["first_half_rank"] == 1:
            first_half = first_half_champions_by_season.setdefault(
                row["season_id"], {"year": row["season_id"], "names": [], "score": None}
            )
            first_half["names"].append(row["name"])
            first_half["score"] = row["first_half_score"]
        if row["second_half_rank"] == 1:
            second_half = second_half_champions_by_season.setdefault(
                row["season_id"], {"year": row["season_id"], "names": [], "score": None}
            )
            second_half["names"].append(row["name"])
            second_half["score"] = row["second_half_score"]

    champions = sorted(champions_by_season.values(), key=lambda c: c["year"])
    first_half_champions = sorted(
        first_half_champions_by_season.values(), key=lambda c: c["year"]
    )
    second_half_champions = sorted(
        second_half_champions_by_season.values(), key=lambda c: c["year"]
    )

    kv = KVClient(**get_kv_config())
    kv.write(
        "meta:historical",
        {
            "years": years,
            "champions": champions,
            "first_half_champions": first_half_champions,
            "second_half_champions": second_half_champions,
            "career": sorted(career.values(), key=lambda c: c["user_id"]),
        },
    )
    logger.info(
        "Wrote meta:historical (%d years, %d career entries) to KV",
        len(years),
        len(career),
    )
