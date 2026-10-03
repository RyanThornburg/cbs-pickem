"""meta:historical - see src/CLAUDE.md's KV writer section.

career_record_by_user() is exported (no leading underscore) because
user_profiles.py's write_user_profiles() needs the same per-user career
record for each user's own profile key."""

import json
import logging
from collections import defaultdict
from typing import Any

from db.clients import get_d1, get_kv
from db.d1_client import D1Client

logger = logging.getLogger(__name__)

# historical season records
_HISTORICAL_SQL = """
SELECT hs.season_id, s.name AS pool_name, s.historical_data_incomplete,
    s.periods_json, u.user_id, u.name, u.is_active, hs.final_rank, hs.final_score,
    hs.first_half_rank, hs.first_half_score,
    hs.second_half_rank, hs.second_half_score, hs.last_place
FROM historical_standings hs
JOIN users u ON u.user_id = hs.user_id
JOIN seasons s ON s.season_id = hs.season_id
ORDER BY hs.season_id, hs.final_rank
"""

_PERIOD_STANDINGS_SQL = """
SELECT season_id, user_id, period_key, rank, score, last_place
FROM historical_period_standings
"""

type PeriodsByUser = dict[tuple[int, int], dict[str, dict[str, Any]]]


def _period_standings(d1: D1Client) -> PeriodsByUser:
    """(season, user) -> {period_key: {rank, score, last_place}}"""
    periods: PeriodsByUser = defaultdict(dict)
    for row in d1.query(_PERIOD_STANDINGS_SQL).results:
        periods[(row["season_id"], row["user_id"])][row["period_key"]] = {
            "rank": row["rank"],
            "score": row["score"],
            "last_place": bool(row["last_place"]),
        }
    return periods


def _period_labels(periods: list[dict[str, Any]] | None) -> dict[str, str]:
    return {period["key"]: period["label"] for period in periods or []}


def _label(key: str, labels: dict[str, str]) -> str:
    # archive seasons have no periods_json - "first_half" -> "First Half"
    return labels.get(key) or key.replace("_", " ").title()


def career_record_by_user(d1: D1Client) -> dict[int, dict[str, Any]]:
    """Per-user career record from historical_standings (prior, closed
    seasons only - the current in-progress season never has a row here
    until season_close_out.py runs at year-end). Shared by write_historical()
    (meta:historical's career list) and src/user_stats.py's
    compute_user_profiles() (each user's own profile key)."""
    return _career_record(d1.query(_HISTORICAL_SQL).results, _period_standings(d1))


def _career_record(
    rows: list[dict[str, Any]], periods: PeriodsByUser
) -> dict[int, dict[str, Any]]:
    """career_record_by_user() from already-fetched _HISTORICAL_SQL rows -
    write_historical() needs the rows itself too, so it queries once"""
    career: dict[int, dict[str, Any]] = {}
    for row in rows:
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
                "last_place": bool(row["last_place"]),
                "periods": periods.get((row["season_id"], row["user_id"]), {}),
            }
        )
    return career


def write_historical() -> None:
    """Write meta:historical - past champions and each user's all-time
    record, from historical_standings (backfilled once from the pre-2026
    archive, and going forward one row per user per season at close-out).
    Static/manual cadence - nothing changes here until a season closes."""
    d1 = get_d1()
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
    # (season, period_key) -> {year, period_key, label, names, score}
    period_champions: dict[tuple[int, str], dict[str, Any]] = {}
    last_places: dict[tuple[int, str], dict[str, Any]] = {}
    period_order: dict[tuple[int, str], int] = {}
    periods = _period_standings(d1)
    career = _career_record(rows, periods)

    def add_winner(
        winners: dict[tuple[int, str], dict[str, Any]],
        season: int,
        key: str,
        labels: dict[str, str],
        name: str,
        score: int | None,
    ) -> None:
        winner = winners.setdefault(
            (season, key),
            {
                "year": season,
                "period_key": key,
                "label": _label(key, labels),
                "names": [],
                "score": score,
            },
        )
        winner["names"].append(name)

    for row in rows:
        season = row["season_id"]
        season_key = str(season)
        season_periods = (
            json.loads(row["periods_json"]) if row["periods_json"] else None
        )
        labels = _period_labels(season_periods)
        for index, period in enumerate(season_periods or []):
            period_order[(season, period["key"])] = index
        user_periods = periods.get((season, row["user_id"]), {})
        year = years.setdefault(
            season_key,
            {
                "pool_name": row["pool_name"],
                "incomplete": bool(row["historical_data_incomplete"]),
                "periods": season_periods,
                "standings": [],
            },
        )
        if row["last_place"]:
            add_winner(
                last_places, season, "overall", labels, row["name"], row["final_score"]
            )
        for key, standing in user_periods.items():
            if standing["rank"] == 1:
                add_winner(
                    period_champions,
                    season,
                    key,
                    labels,
                    row["name"],
                    standing["score"],
                )
            if standing["last_place"]:
                add_winner(
                    last_places, season, key, labels, row["name"], standing["score"]
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
                "last_place": bool(row["last_place"]),
                "periods": user_periods,
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

    def season_order(winner: dict[str, Any]) -> tuple[int, int, str]:
        # the season's own period order, overall first; archive seasons by key
        key = winner["period_key"]
        position = -1 if key == "overall" else period_order.get((winner["year"], key))
        return winner["year"], position if position is not None else 99, key

    get_kv().write(
        "meta:historical",
        {
            "years": years,
            "champions": champions,
            "first_half_champions": first_half_champions,
            "second_half_champions": second_half_champions,
            "period_champions": sorted(period_champions.values(), key=season_order),
            "last_place": sorted(last_places.values(), key=season_order),
            "career": sorted(career.values(), key=lambda c: c["user_id"]),
        },
    )
    logger.info(
        "Wrote meta:historical (%d years, %d career entries) to KV",
        len(years),
        len(career),
    )
