"""week:{season}:{weekNN}:leaderboard + compute_week_leaderboard() - also
reused directly by src/season_close_out.py for the season's final
standings, see src/CLAUDE.md's KV writer/Season close-out sections."""

import logging
from collections import defaultdict
from typing import Any

from config.config import (
    FIRST_HALF_PAID_PLACES,
    OVERALL_PAID_PLACES,
    SEASON,
    SECOND_HALF_PAID_PLACES,
    SECOND_HALF_START_WEEK,
    get_d1_config,
    get_kv_config,
)
from db.d1_client import D1Client
from db.kv_client import KVClient
from src.kv_writer.shared import PAID_PLACES, resolve_current_week

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


def _standard_rank(score_by_user: dict[int, int]) -> dict[int, int]:
    """highest first, ties cause next number to be skipped"""
    ranked = sorted(score_by_user.items(), key=lambda item: -item[1])
    rank_by_user: dict[int, int] = {}
    prev_score: int | None = None
    prev_rank = 0
    for i, (user_id, score) in enumerate(ranked, start=1):
        if score != prev_score:
            prev_rank = i
            prev_score = score
        rank_by_user[user_id] = prev_rank
    return rank_by_user


def _in_money(place: int | None, paid_places: int) -> bool:
    return place is not None and place <= paid_places


def _prior_seasons_by_user(d1: D1Client) -> dict[int, int]:
    return {
        row["user_id"]: row["prior_seasons"]
        for row in d1.query(_SEASONS_PLAYED_SQL).results
    }


def compute_week_leaderboard(
    d1: D1Client, week_number: int
) -> list[dict[str, Any]] | None:
    """
    overall and second half standings + current week
    returning ranked / ties
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

    in_second_half = week_number >= SECOND_HALF_START_WEEK

    names: dict[int, str] = {}
    weekly_score: dict[int, int] = {}
    trending_score: dict[int, int] = {}
    cumulative_score: dict[int, int] = {}
    first_half_score: dict[int, int] = {}
    second_half_score: dict[int, int] = {}
    has_submitted_picks: dict[int, bool] = {}

    for row in performance_rows:
        user_id = row["user_id"]
        names[user_id] = row["name"]
        picks_correct = row["picks_correct"] or 0
        cumulative_score[user_id] = cumulative_score.get(user_id, 0) + picks_correct
        if row["week_number"] < SECOND_HALF_START_WEEK:
            first_half_score[user_id] = first_half_score.get(user_id, 0) + picks_correct
        elif in_second_half:
            second_half_score[user_id] = (
                second_half_score.get(user_id, 0) + picks_correct
            )
        if row["week_number"] == week_number:
            weekly_score[user_id] = picks_correct
            trending_score[user_id] = row["trending_score"] or 0
            has_submitted_picks[user_id] = bool(row["has_submitted_picks"])

    place = _standard_rank(cumulative_score)
    first_half_place = _standard_rank(first_half_score)
    second_half_place = _standard_rank(second_half_score) if in_second_half else {}

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

    users_json: list[dict[str, Any]] = [
        {
            "user_id": user_id,
            "name": names[user_id],
            # +1 for the current season itself because historical records don't have currrent season
            "seasons_played": prior_seasons_by_user.get(user_id, 0) + 1,
            "weekly_score": weekly_score.get(user_id, 0),
            "trending_score": trending_score.get(user_id, 0),
            "cumulative_score": cumulative_score[user_id],
            "place": place[user_id],
            "first_half_score": first_half_score.get(user_id),
            "first_half_place": first_half_place.get(user_id),
            "second_half_score": second_half_score.get(user_id)
            if in_second_half
            else None,
            "second_half_place": second_half_place.get(user_id)
            if in_second_half
            else None,
            "in_money_overall": _in_money(place.get(user_id), OVERALL_PAID_PLACES),
            "in_money_first_half": _in_money(
                first_half_place.get(user_id), FIRST_HALF_PAID_PLACES
            ),
            "in_money_second_half": in_second_half
            and _in_money(second_half_place.get(user_id), SECOND_HALF_PAID_PLACES),
            "has_submitted_picks": has_submitted_picks.get(user_id, False),
            "picks": picks_by_user.get(user_id, []),
        }
        for user_id in cumulative_score
    ]
    users_json.sort(key=lambda u: u["place"])
    return users_json


def write_week_leaderboard(week_number: int) -> None:
    """Write week:{season}:{weekNN}:leaderboard from compute_week_leaderboard()."""
    d1 = D1Client(**get_d1_config())
    users_json = compute_week_leaderboard(d1, week_number)
    if users_json is None:
        logger.warning(
            "No weekly_performance for season %s week %s - not writing leaderboard key",
            SEASON,
            week_number,
        )
        return

    kv = KVClient(**get_kv_config())
    kv.write(
        f"week:{SEASON}:{week_number:02d}:leaderboard",
        {
            "week": week_number,
            "second_half_start_week": SECOND_HALF_START_WEEK,
            "paid_places": PAID_PLACES,
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
    d1 = D1Client(**get_d1_config())
    current_week = resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s - not writing leaderboard key",
            SEASON,
        )
        return

    write_week_leaderboard(current_week)
