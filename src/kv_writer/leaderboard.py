"""week:{season}:{weekNN}:leaderboard + compute_week_leaderboard() - also
reused directly by src/season_close_out.py for the season's final
standings, see src/CLAUDE.md's KV writer/Season close-out sections."""

import logging
from collections import defaultdict
from typing import Any

from config.config import PERIODS, SEASON, Period
from db.clients import get_d1, get_kv
from db.d1_client import D1Client
from src.game_rules import standard_rank
from src.kv_writer.shared import (
    LEGACY_PAID_PLACES,
    LEGACY_SECOND_HALF_START_WEEK,
    for_current_week,
)
from src.periods import final_week_number, period_definitions

logger = logging.getLogger(__name__)

# calculate vs adding a running total in db
_WEEKLY_PERFORMANCE_SQL = """
SELECT wp.user_id, u.name, w.week_number, wp.picks_correct, wp.trending_score,
    wp.has_submitted_picks
FROM weekly_performance wp
JOIN weeks w ON w.week_id = wp.week_id
JOIN users u ON u.user_id = wp.user_id
WHERE w.season_id = ? AND w.week_number <= ? AND u.is_active = TRUE
"""

_LEADERBOARD_PICKS_SQL = """
SELECT up.user_id, up.game_id, up.picked_team_id, up.is_correct, up.trending_status
FROM user_picks up
JOIN games g ON g.game_id = up.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
"""

# prior seasons count of a user from historical standings
_SEASONS_PLAYED_SQL = """
SELECT user_id, COUNT(DISTINCT season_id) AS prior_seasons
FROM historical_standings
GROUP BY user_id
"""


def _in_money(place: int | None, paid_places: int) -> bool:
    return place is not None and place <= paid_places


def _period_entry(
    period: Period, score: int | None, place: int | None
) -> dict[str, Any]:
    return {
        "score": score,
        "place": place,
        "in_money": _in_money(place, period.paid_places),
    }


def _legacy_period_fields(periods: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The pre-`periods` per-user fields (first_half_score, in_money_overall,
    ...), kept until the UI reads `periods` - remove with LEGACY_PAID_PLACES"""
    fields: dict[str, Any] = {"in_money_overall": periods["overall"]["in_money"]}
    for key in ("first_half", "second_half"):
        entry = periods.get(key, {})
        fields[f"{key}_score"] = entry.get("score")
        fields[f"{key}_place"] = entry.get("place")
        fields[f"in_money_{key}"] = entry.get("in_money", False)
    return fields


def _prior_seasons_by_user(d1: D1Client) -> dict[int, int]:
    return {
        row["user_id"]: row["prior_seasons"]
        for row in d1.query(_SEASONS_PLAYED_SQL).results
    }


def compute_week_leaderboard(
    d1: D1Client, week_number: int
) -> list[dict[str, Any]] | None:
    """
    overall standings, every config.PERIODS standings and the current week,
    ranked with ties
    using cbs status is_correct/trending_status/trending_score instead
    of calculating the actual results
    """
    performance_rows = d1.query(_WEEKLY_PERFORMANCE_SQL, [SEASON, week_number]).results
    if not performance_rows:
        logger.warning(
            "No weekly_performance found through season %s week %s",
            SEASON,
            week_number,
        )
        return None

    names: dict[int, str] = {}
    weekly_score: dict[int, int] = {}
    trending_score: dict[int, int] = {}
    cumulative_score: dict[int, int] = {}
    # a period that hasn't started yet stays empty - every user's score is None
    period_score: dict[str, dict[int, int]] = {period.key: {} for period in PERIODS}
    has_submitted_picks: dict[int, bool] = {}

    for row in performance_rows:
        user_id = row["user_id"]
        names[user_id] = row["name"]
        picks_correct = row["picks_correct"] or 0
        cumulative_score[user_id] = cumulative_score.get(user_id, 0) + picks_correct
        for period in PERIODS:
            if period.covers(row["week_number"]):
                scores = period_score[period.key]
                scores[user_id] = scores.get(user_id, 0) + picks_correct
        if row["week_number"] == week_number:
            weekly_score[user_id] = picks_correct
            trending_score[user_id] = row["trending_score"] or 0
            has_submitted_picks[user_id] = bool(row["has_submitted_picks"])

    place = standard_rank(cumulative_score)
    period_place = {key: standard_rank(scores) for key, scores in period_score.items()}

    prior_seasons_by_user = _prior_seasons_by_user(d1)

    picks_by_user: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in d1.query(_LEADERBOARD_PICKS_SQL, [SEASON, week_number]).results:
        picks_by_user[pick["user_id"]].append(
            {
                "game_id": pick["game_id"],
                "team_id": pick["picked_team_id"],
                "is_correct": (
                    None if pick["is_correct"] is None else bool(pick["is_correct"])
                ),
                "trending_status": pick["trending_status"],
            }
        )

    users_json: list[dict[str, Any]] = []
    for user_id, user_cumulative in cumulative_score.items():
        periods = {
            period.key: _period_entry(
                period,
                period_score[period.key].get(user_id),
                period_place[period.key].get(user_id),
            )
            for period in PERIODS
        }
        users_json.append(
            {
                "user_id": user_id,
                "name": names[user_id],
                # +1 for the current season itself because historical records don't have currrent season
                "seasons_played": prior_seasons_by_user.get(user_id, 0) + 1,
                "weekly_score": weekly_score.get(user_id, 0),
                "trending_score": trending_score.get(user_id, 0),
                "cumulative_score": user_cumulative,
                "place": place[user_id],
                "periods": periods,
                **_legacy_period_fields(periods),
                "has_submitted_picks": has_submitted_picks.get(user_id, False),
                "picks": picks_by_user.get(user_id, []),
            }
        )
    users_json.sort(key=lambda u: u["place"])
    return users_json


def write_week_leaderboard(week_number: int) -> None:
    """Write week:{season}:{weekNN}:leaderboard from compute_week_leaderboard()."""
    d1 = get_d1()
    users_json = compute_week_leaderboard(d1, week_number)
    if users_json is None:
        logger.warning(
            "No weekly_performance for season %s week %s - not writing leaderboard key",
            SEASON,
            week_number,
        )
        return

    get_kv().write(
        f"week:{SEASON}:{week_number:02d}:leaderboard",
        {
            "week": week_number,
            "periods": period_definitions(final_week_number(d1)),
            "second_half_start_week": LEGACY_SECOND_HALF_START_WEEK,
            "paid_places": LEGACY_PAID_PLACES,
            "users": users_json,
        },
    )
    logger.info(
        "Wrote week:%s:%02d:leaderboard (%d users) to KV",
        SEASON,
        week_number,
        len(users_json),
    )


def write_current_week_leaderboard() -> None:
    """Resolve weeks.is_current and write that week's leaderboard key."""
    for_current_week(write_week_leaderboard, "leaderboard key")
