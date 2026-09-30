"""week:{season}:{weekNN}:recap - see src/CLAUDE.md's KV writer section.

Short "did you know" items for the web UI's weekly infographic. Each
generator below returns zero or more candidate recap items, and the key holds
all of them ranked by `score`, highest first - the UI rotates through the
top few. The always-on kinds (pool accuracy, spread mattered, chaos index,
...) carry a hand-picked base score nudged up when the week is extreme.
The split kinds (home/road, favorites/underdogs, kickoff slot, division
games) only show up at all once they clear _stands_out(): enough sample
and far enough from a coin flip that it's a real lean, not noise. Early
in a season most splits won't qualify, which is the point.

Everything is computed "as of" the key's week (season data through that
week only), so rewriting an old week's key gives the same answer it would
have had at the time, apart from late grading."""

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from config.config import SEASON
from db.clients import get_d1, get_kv
from db.d1_client import D1Client
from src.kv_writer.recap import accuracy, chaos, crowd, splits, streaks
from src.kv_writer.recap.common import Season
from src.kv_writer.shared import for_current_week
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

# Bump whenever a kind is renamed or removed, or a field changes shape, so
# the UI can tell a stale card mapping from a real change - see the
# changelog in the UI reference Artifact linked from CLAUDE.local.md.
# 1: first version. 2: consensus_* -> crowd_record/popular_picks, people as
# {user_id, name}, short, movers/cover_streaks lists, league category.

SCHEMA_VERSION = 3
# a week keeps getting rewritten this long after its last kickoff, so its
# final state (last game FINAL, CBS's last grades) lands even after
# weeks.is_current has moved on
_RECENT_WEEK_HOURS = 12

_SEASON_GAMES_SQL = """
SELECT g.game_id, w.week_number, g.status, g.home_score, g.away_score, g.cbs_spread,
    g.game_time, g.neutral_site,
    ht.team_id AS home_id, ht.abbreviation AS home_abbr, ht.nick_name AS home_name,
    ht.division AS home_division,
    at.team_id AS away_id, at.abbreviation AS away_abbr, at.nick_name AS away_name,
    at.division AS away_division
FROM games g
JOIN teams ht ON ht.team_id = g.home_team_id
JOIN teams at ON at.team_id = g.away_team_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number <= ?
ORDER BY g.game_time
"""

_SEASON_PICKS_SQL = """
SELECT up.game_id, up.user_id, u.name, up.picked_team_id, up.is_correct, w.week_number
FROM user_picks up
JOIN users u ON u.user_id = up.user_id
JOIN games g ON g.game_id = up.game_id
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number <= ? AND u.is_active = TRUE
"""

# weeks.start_time/end_time are the first/last kickoff, ISO8601 UTC text,
# so plain string comparison works
_RECENT_WEEKS_SQL = """
SELECT week_number FROM weeks
WHERE season_id = ?
    AND (is_current = 1 OR (start_time <= ? AND (is_complete = 0 OR end_time >= ?)))
ORDER BY week_number
"""

# same source and filter as leaderboard.py, so mover ranks match the leaderboard's
_SEASON_PERFORMANCE_SQL = """
SELECT wp.user_id, u.name, w.week_number, wp.picks_correct
FROM weekly_performance wp
JOIN weeks w ON w.week_id = wp.week_id
JOIN users u ON u.user_id = wp.user_id
WHERE w.season_id = ? AND w.week_number <= ? AND u.is_active = TRUE
"""


def compute_week_recap(d1: D1Client, week_number: int) -> dict[str, Any] | None:
    games = d1.query(_SEASON_GAMES_SQL, [SEASON, week_number]).results
    if not any(g["week_number"] == week_number for g in games):
        return None

    picks_by_game: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for pick in d1.query(_SEASON_PICKS_SQL, [SEASON, week_number]).results:
        picks_by_game[pick["game_id"]].append(pick)
    performance = d1.query(_SEASON_PERFORMANCE_SQL, [SEASON, week_number]).results

    season = Season(week_number, games, picks_by_game, performance)
    week_games = season.week_games(week_number)
    games_final = sum(1 for g in week_games if g["status"] == "FINAL")
    week_complete = games_final == len(week_games)

    pool_series = accuracy.pool_accuracy_series(season)
    chaos_series = chaos.chaos_series(season, pool_series)

    moves = streaks.rank_moves(season)
    cover_streaks = streaks.active_cover_streaks(season)

    items = [
        *accuracy.pool_accuracy_items(season, pool_series, week_complete),
        *accuracy.spread_mattered_items(season),
        *crowd.crowd_items(season),
        *chaos.chaos_items(season, chaos_series),
        *crowd.twins_and_oppos_items(season),
        *streaks.cover_streak_items(cover_streaks),
        *streaks.biggest_mover_items(moves),
        *streaks.upset_items(season),
        *splits.game_split_items(season),
        *splits.pool_split_items(season),
        *splits.team_split_items(season),
    ]
    items.sort(key=lambda t: -t["score"])

    return {
        "version": SCHEMA_VERSION,
        "season": SEASON,
        "week": week_number,
        "updated_at": utc_iso(),
        "week_complete": week_complete,
        "games_final": games_final,
        "games_total": len(week_games),
        "items": items,
        "series": {
            "pool_accuracy": pool_series,
            "chaos": chaos_series,
        },
        # every leaderboard move of streaks.MIN_RANK_MOVE+ places (the mover
        # recap items only headline the biggest), for arrows on each row
        "movers": [m for m in moves if abs(m["change"]) >= streaks.MIN_RANK_MOVE],
        # every active team streak of _MIN_COVER_STREAK+, for game badges
        "cover_streaks": cover_streaks,
    }


def write_week_recap(week_number: int) -> None:
    """Write week:{season}:{weekNN}:recap from compute_week_recap()."""
    d1 = get_d1()
    payload = compute_week_recap(d1, week_number)
    if payload is None:
        logger.warning(
            "No games found for season %s week %s - not writing recap key",
            SEASON,
            week_number,
        )
        return

    get_kv().write(f"week:{SEASON}:{week_number:02d}:recap", payload)
    logger.info(
        "Wrote week:%s:%02d:recap (%d items) to KV",
        SEASON,
        week_number,
        len(payload["items"]),
    )


def write_current_week_recap() -> None:
    """Resolve weeks.is_current and write that week's recap key."""
    for_current_week(write_week_recap, "recap key")


def write_recent_weeks_recap() -> None:
    """What orchestration refreshes: the current week, any week that has
    started but isn't complete, and any week whose last kickoff was within
    _RECENT_WEEK_HOURS. CBS can move weeks.is_current on before Monday
    night's game ends, and the pool's grades (CBS's is_correct) land a
    poll or two after a game goes FINAL - writing only the current week
    lost the old week's final state (final chaos index, "nobody went 5-0",
    final accuracy)."""
    d1 = get_d1()
    now = datetime.now(UTC)
    rows = d1.query(
        _RECENT_WEEKS_SQL,
        [
            SEASON,
            utc_iso(now),
            utc_iso(now - timedelta(hours=_RECENT_WEEK_HOURS)),
        ],
    ).results
    if not rows:
        logger.info(
            "No current or recent weeks for season %s - no recap to write", SEASON
        )
        return
    for row in rows:
        write_week_recap(row["week_number"])
