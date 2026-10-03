"""config.PERIODS helpers shared by the leaderboard, meta:current, user
profiles and season close-out - outside kv_writer so src/user_stats.py can
import it without a cycle (same reason as src/game_rules.py)."""

from typing import Any

from config.config import PERIODS, SEASON, Period
from db.d1_client import D1Client

_FINAL_WEEK_SQL = "SELECT MAX(week_number) AS final_week FROM weeks WHERE season_id = ?"


def final_week_number(d1: D1Client) -> int | None:
    """config.SEASON's last week number, None before its weeks load"""
    result = d1.query(_FINAL_WEEK_SQL, [SEASON]).results
    return result[0]["final_week"] if result else None


def period_end(period: Period, final_week: int | None) -> int | None:
    return final_week if period.end_week is None else period.end_week


def period_definitions(final_week: int | None) -> list[dict[str, Any]]:
    """config.PERIODS as the KV keys and seasons.periods_json show them,
    end_week resolved to the season's last week"""
    return [
        {
            "key": period.key,
            "label": period.label,
            "start_week": period.start_week,
            "end_week": period_end(period, final_week),
            "paid_places": period.paid_places,
            "pay_last_place": period.pay_last_place,
        }
        for period in PERIODS
    ]
