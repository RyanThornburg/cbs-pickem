"""SQL/helpers genuinely used by more than one src/kv_writer/ submodule -
see src/CLAUDE.md's KV writer section. Domain-specific SQL/helpers stay in
their own module even when there's only one caller; something only moves
here once a second module actually needs it (same "consolidate once two
callers need it" bar as loader_helper.py, see src/CLAUDE.md's Loaders
section).

write_meta_current() lives here rather than in its own module - meta:current
is just resolve_current_week() plus a couple of pool-rule constants, not
worth a dedicated file."""

import logging
from datetime import UTC, datetime
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

logger = logging.getLogger(__name__)

PAID_PLACES = {
    "overall": OVERALL_PAID_PLACES,
    "first_half": FIRST_HALF_PAID_PLACES,
    "second_half": SECOND_HALF_PAID_PLACES,
}

# Shared by games.py (write_week_games) and trends.py (write_week_trends) -
# the exact same query, not duplicated on purpose.
GAMES_SQL = """
SELECT g.game_id, g.status, g.status_desc, g.home_score, g.away_score, g.game_time,
    g.cbs_spread, g.tv_network, g.gametracker_url, g.neutral_site,
    g.forecast_temp_f, g.forecast_feels_like_f, g.forecast_condition, g.forecast_icon,
    g.forecast_precip_type, g.forecast_wind_speed_mph, g.forecast_wind_gust_mph,
    g.forecast_wind_direction, g.forecast_precipitation_pct,
    g.forecast_visibility_mi, g.forecast_alert, g.forecast_captured_at,
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


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_current_week(d1: D1Client) -> int | None:
    """weeks.is_current"""
    result = d1.query(
        "SELECT week_number FROM weeks WHERE season_id = ? AND is_current = 1",
        [SEASON],
    )
    return result.results[0]["week_number"] if result.results else None


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
    d1 = D1Client(**get_d1_config())
    current_week = resolve_current_week(d1)
    if current_week is None:
        logger.warning(
            "No current week found for season %s - not writing meta:current",
            SEASON,
        )
        return

    kv = KVClient(**get_kv_config())
    kv.write(
        "meta:current",
        {
            "season": SEASON,
            "current_week": current_week,
            "second_half_start_week": SECOND_HALF_START_WEEK,
            "paid_places": PAID_PLACES,
        },
    )
    logger.info("Wrote meta:current (week %d) to KV", current_week)
