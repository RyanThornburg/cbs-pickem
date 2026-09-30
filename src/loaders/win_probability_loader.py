"""
Capture ESPN's full per-play win probability curve for finished games into
game_win_probability - once per game, after it goes FINAL. The live value
is already on every game_snapshots row (home_win_pct); this is the
complete, gap-free curve from ESPN's summary endpoint, which only needs
fetching once the game is over.

ESPN is undocumented/extra here, same as its scoreboard: a failed fetch is
logged and retried on a later tick, never fatal.

Usage:
  uv run python -m src.loaders.win_probability_loader [local|prod]
    (recently FINAL games that don't have a curve yet)
  uv run python -m src.loaders.win_probability_loader [local|prod] <week_number>
    (every FINAL game in that week, replacing any stored curve)
"""

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from api.espn_client import get_summary
from api.espn_models import Summary
from config.config import SEASON, configure_logging, load_env
from db.clients import get_d1
from db.d1_client import D1Client
from src.loaders.loader_helper import sql_batch_call

logger = logging.getLogger(__name__)

# how long after kickoff a FINAL game without a curve keeps being retried -
# bounds the retries if ESPN never has one for some game
RETRY_WINDOW_DAYS = 3

_GAMES_MISSING_SQL = """
SELECT g.game_id, g.espn_event_id
FROM games g
WHERE g.status = 'FINAL' AND g.espn_event_id IS NOT NULL AND g.game_time >= ?
  AND NOT EXISTS (SELECT 1 FROM game_win_probability p WHERE p.game_id = g.game_id)
"""

_WEEK_GAMES_SQL = """
SELECT g.game_id, g.espn_event_id
FROM games g
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ?
  AND g.status = 'FINAL' AND g.espn_event_id IS NOT NULL
"""

_UPSERT_SQL = """
INSERT INTO game_win_probability (game_id, points_json, captured_at)
VALUES (?, ?, CURRENT_TIMESTAMP)
ON CONFLICT(game_id) DO UPDATE SET
    points_json = excluded.points_json,
    captured_at = excluded.captured_at
"""


def _points(summary: Summary) -> list[dict[str, Any]]:
    """one point per win probability entry, joined to its play for the
    period/clock/score. The pre-kickoff point matches no play - it gets
    period 0, no clock and a 0-0 score so it still charts as the start."""
    drives = summary.drives
    plays = {
        play.id: play
        for drive in (
            [*drives.previous, *([drives.current] if drives.current else [])]
            if drives
            else []
        )
        for play in drive.plays
    }

    points: list[dict[str, Any]] = []
    for point in summary.win_probability:
        play = plays.get(point.play_id)
        points.append(
            {
                "period": play.period.number if play and play.period else 0,
                "clock": play.clock.display_value if play and play.clock else None,
                "home_win_pct": round(point.home_win_percentage * 100, 1),
                "home_score": play.home_score if play else 0,
                "away_score": play.away_score if play else 0,
                "scoring_play": play.scoring_play if play else False,
            }
        )
    return points


def _load_for_games(games: list[dict[str, Any]], client: D1Client) -> set[int]:
    """fetch and store each game's curve - returns the game_ids stored"""
    statements: list[tuple[str, list[Any] | None]] = []
    stored: set[int] = set()
    for game in games:
        try:
            summary = get_summary(game["espn_event_id"])
        except Exception:
            logger.exception(
                "ESPN summary fetch failed for game_id=%s - retrying later",
                game["game_id"],
            )
            continue
        points = _points(summary)
        if not points:
            logger.warning(
                "No ESPN win probability for game_id=%s yet", game["game_id"]
            )
            continue
        statements.append(
            (_UPSERT_SQL, [game["game_id"], json.dumps(points, separators=(",", ":"))])
        )
        stored.add(game["game_id"])

    if statements:
        sql_batch_call(statements, client)
    if games:
        logger.info(
            "Stored win probability for %d of %d game(s)", len(stored), len(games)
        )
    return stored


def load_final_win_probability() -> set[int]:
    """recently FINAL games without a curve yet - one D1 query and no ESPN
    calls when there's nothing to do, so it's fine to call every tick"""
    client = get_d1()
    cutoff = (datetime.now(UTC) - timedelta(days=RETRY_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return _load_for_games(client.query(_GAMES_MISSING_SQL, [cutoff]).results, client)


def backfill_week_win_probability(week_number: int) -> set[int]:
    """every FINAL game in a week, replacing any stored curve"""
    client = get_d1()
    return _load_for_games(
        client.query(_WEEK_GAMES_SQL, [SEASON, week_number]).results, client
    )


def main() -> None:
    """win probability - a week number backfills that week"""
    if len(sys.argv) > 2:
        backfill_week_win_probability(int(sys.argv[2]))
    else:
        load_final_win_probability()


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    main()
