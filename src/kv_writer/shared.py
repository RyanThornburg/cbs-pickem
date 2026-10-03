"""SQL/helpers genuinely used by more than one src/kv_writer/ submodule -
see src/CLAUDE.md's KV writer section. Domain-specific SQL/helpers stay in
their own module even when there's only one caller; something only moves
here once a second module actually needs it (same "consolidate once two
callers need it" bar as loader_helper.py, see src/CLAUDE.md's Loaders
section).

write_meta_current() lives here rather than in its own module - meta:current
is just resolve_current_week() plus a couple of pool-rule constants and the
CBS pool link, not worth a dedicated file."""

import logging
from collections.abc import Callable
from typing import Any

from api.cbs_client import cbs_pool_url
from config.config import PERIODS_BY_KEY, SEASON, get_cbs_config
from db.clients import get_d1, get_kv
from db.d1_client import D1Client
from src.periods import final_week_number, period_definitions

logger = logging.getLogger(__name__)

# The pre-`periods` fields (meta:current/leaderboard `paid_places` and
# `second_half_start_week`), kept until the UI reads `periods` - remove
# them together. Only meaningful while PERIODS has the two halves.
LEGACY_PAID_PLACES = {
    key: PERIODS_BY_KEY[key].paid_places
    for key in ("overall", "first_half", "second_half")
    if key in PERIODS_BY_KEY
}
LEGACY_SECOND_HALF_START_WEEK = (
    PERIODS_BY_KEY["second_half"].start_week
    if "second_half" in PERIODS_BY_KEY
    else None
)

# Shared by games.py (write_week_games) and trends.py (write_week_trends) -
# the exact same query, not duplicated on purpose.
GAMES_SQL = """
SELECT g.game_id, g.status, g.status_desc, g.home_score, g.away_score, g.game_time,
    g.home_q1_score, g.home_q2_score, g.home_q3_score, g.home_q4_score, g.home_ot_score,
    g.away_q1_score, g.away_q2_score, g.away_q3_score, g.away_q4_score, g.away_ot_score,
    g.cbs_spread, g.tv_network, g.gametracker_url, g.neutral_site,
    g.forecast_temp_f, g.forecast_feels_like_f, g.forecast_condition, g.forecast_icon,
    g.forecast_precip_type, g.forecast_wind_speed_mph, g.forecast_wind_gust_mph,
    g.forecast_wind_direction, g.forecast_precipitation_pct,
    g.forecast_visibility_mi, g.forecast_alerts_json, g.forecast_captured_at,
    g.forecast_window_precip_pct_max, g.forecast_window_precip_type,
    g.forecast_window_wind_gust_mph_max, g.forecast_window_temp_f_low,
    g.forecast_window_temp_f_high, g.forecast_window_snow_accum_in, g.forecast_source,
    g.forecast_hours_json,
    s.stadium_id, s.name AS stadium_name, s.city AS stadium_city,
    s.state AS stadium_state, s.country AS stadium_country,
    s.latitude AS stadium_latitude, s.longitude AS stadium_longitude,
    s.roof_type AS stadium_roof_type, s.surface_type AS stadium_surface_type,
    ht.team_id AS home_id, ht.abbreviation AS home_abbr, ht.nick_name AS home_name,
    ht.wins AS home_wins, ht.losses AS home_losses, ht.ties AS home_ties,
    at.team_id AS away_id, at.abbreviation AS away_abbr, at.nick_name AS away_name,
    at.wins AS away_wins, at.losses AS away_losses, at.ties AS away_ties
FROM games g
JOIN teams ht ON ht.team_id = g.home_team_id
JOIN teams at ON at.team_id = g.away_team_id
JOIN weeks w ON w.week_id = g.week_id
LEFT JOIN stadiums s ON s.stadium_id = g.stadium_id
WHERE w.season_id = ? AND w.week_number = ?
ORDER BY g.game_time
"""

PICKS_SQL = """
SELECT up.game_id, up.user_id, u.name, up.picked_team_id
FROM user_picks up
JOIN users u ON u.user_id = up.user_id
JOIN games g ON g.game_id = up.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
"""


def resolve_current_week(d1: D1Client) -> int | None:
    """weeks.is_current"""
    result = d1.query(
        "SELECT week_number FROM weeks WHERE season_id = ? AND is_current = 1",
        [SEASON],
    )
    return result.results[0]["week_number"] if result.results else None


def for_current_week(write_week: Callable[[int], None], label: str) -> None:
    """Resolve weeks.is_current and run write_week() for it - the body of
    every write_current_week_*(). `label` names the key in the warning."""
    current_week = resolve_current_week(get_d1())
    if current_week is None:
        logger.warning(
            "No current week found for season %s - not writing %s", SEASON, label
        )
        return
    write_week(current_week)


def game_team_dicts(game: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {"id": game["home_id"], "abbr": game["home_abbr"], "name": game["home_name"]},
        {"id": game["away_id"], "abbr": game["away_abbr"], "name": game["away_name"]},
    )


def split_home_away(
    game: dict[str, Any], picks: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    home = [p for p in picks if p["picked_team_id"] == game["home_id"]]
    away = [p for p in picks if p["picked_team_id"] == game["away_id"]]
    return home, away


def write_meta_current() -> None:
    """Write meta:current - which week is live right now, for this season."""
    d1 = get_d1()
    current_week = resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s - not writing meta:current",
            SEASON,
        )
        return

    get_kv().write(
        "meta:current",
        {
            "season": SEASON,
            "current_week": current_week,
            "periods": period_definitions(final_week_number(d1)),
            "second_half_start_week": LEGACY_SECOND_HALF_START_WEEK,
            "paid_places": LEGACY_PAID_PLACES,
            "cbs_pool_url": cbs_pool_url(get_cbs_config().pool_id),
        },
    )
    logger.info("Wrote meta:current (week %d) to KV", current_week)
