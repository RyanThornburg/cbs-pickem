"""
Load scoring plays (Sports IO /games/events) into game_scoring_plays.

Only fetches a game whose latest stored play doesn't match its current
games.home_score/away_score - i.e. someone scored since the last fetch (the
Sports IO live poll keeps those scores current). That makes it cheap enough
to call every tick: with nothing new it's a single D1 query, no API calls.

Usage:
  uv run python -m src.loaders.scoring_plays_loader [local|prod]
  uv run python -m src.loaders.scoring_plays_loader [local|prod] <week_number>
    (backfill every game in that week, regardless of the score check)
"""

import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from api.sports_io_client import get_game_events
from config.config import SEASON, configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.loaders.loader_helper import id_map, mapping_gap_statement, sql_batch_call

logger = logging.getLogger(__name__)

# a FINAL game is still checked for this long after kickoff, in case its
# last score landed after the game flipped to FINAL
FINAL_RECHECK_HOURS = 6

_QUARTERS = {"First": 1, "Second": 2, "Third": 3, "Fourth": 4, "Overtime": 5}

# games that need a fetch: live (or recently FINAL) with points on the board,
# and whose latest stored play isn't at the current score. `IS NOT` so a game
# with no plays stored yet (NULL) counts as behind too.
_GAMES_BEHIND_SQL = """
SELECT g.game_id, g.sports_io_game_id, g.home_score, g.away_score
FROM games g
WHERE g.sports_io_game_id IS NOT NULL
  AND (
    g.status IN ('IN_PROGRESS', 'HALFTIME', 'DELAYED')
    OR (g.status = 'FINAL' AND g.game_time >= ?)
  )
  AND COALESCE(g.home_score, 0) + COALESCE(g.away_score, 0) > 0
  AND (
    SELECT p.home_score || '-' || p.away_score
    FROM game_scoring_plays p
    WHERE p.game_id = g.game_id
    ORDER BY p.sequence DESC
    LIMIT 1
  ) IS NOT (g.home_score || '-' || g.away_score)
"""

_WEEK_GAMES_SQL = """
SELECT g.game_id, g.sports_io_game_id, g.home_score, g.away_score
FROM games g
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ? AND g.sports_io_game_id IS NOT NULL
"""

_DELETE_PLAYS_SQL = "DELETE FROM game_scoring_plays WHERE game_id = ?"

_INSERT_PLAY_SQL = """
INSERT INTO game_scoring_plays (
    game_id, sequence, quarter, clock, team_id, type, description,
    player_name, home_score, away_score
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _load_plays_for_games(games: list[dict[str, Any]], client: D1Client) -> None:
    """Replace each game's stored plays with Sports IO's current list. A
    failed fetch for one game is logged and skipped, not fatal - scoring
    plays are enrichment, same as weather."""
    team_ids = id_map(client, "teams", "sports_io_team_id", "team_id")

    statements: list[tuple[str, list[Any] | None]] = []
    gap_statements: list[tuple[str, list[Any] | None]] = []
    loaded = 0
    for game in games:
        try:
            events = get_game_events(game["sports_io_game_id"])
        except Exception:
            logger.exception(
                "Scoring plays fetch failed for game_id=%s - skipping", game["game_id"]
            )
            continue

        if not events:
            # Sports IO lags the score slightly - leave whatever is stored
            # and try again next tick rather than wiping it
            continue

        statements.append((_DELETE_PLAYS_SQL, [game["game_id"]]))
        for sequence, event in enumerate(events, start=1):
            quarter = _QUARTERS.get(event.quarter)
            if quarter is None:
                logger.warning(
                    "Unknown Sports IO quarter %r for game_id=%s",
                    event.quarter,
                    game["game_id"],
                )
                gap_statements.append(
                    mapping_gap_statement(
                        "sports_io", "event_quarter", event.quarter, "load_scoring_plays"
                    )
                )

            team_id = team_ids.get(event.team.id)
            if team_id is None:
                logger.warning(
                    "No teams row for sports_io team %s (game_id=%s)",
                    event.team.id,
                    game["game_id"],
                )
                gap_statements.append(
                    mapping_gap_statement(
                        "sports_io", "team", event.team.id, "load_scoring_plays"
                    )
                )

            statements.append(
                (
                    _INSERT_PLAY_SQL,
                    [
                        game["game_id"],
                        sequence,
                        quarter,
                        event.minute,
                        team_id,
                        event.type,
                        event.comment,
                        event.player.name,
                        event.score.home,
                        event.score.away,
                    ],
                )
            )
        loaded += 1

        last = events[-1].score
        if (last.home, last.away) != (game["home_score"], game["away_score"]):
            # normal for a tick or two while Sports IO's events catch up
            logger.info(
                "game_id=%s: latest scoring play is %s-%s, games row says %s-%s",
                game["game_id"],
                last.home,
                last.away,
                game["home_score"],
                game["away_score"],
            )

    if statements or gap_statements:
        sql_batch_call(statements + gap_statements, client)
    logger.info("Loaded scoring plays for %d of %d game(s)", loaded, len(games))


def load_scoring_plays() -> None:
    """Refresh scoring plays for every game whose score moved since the
    last fetch - see the module docstring."""
    client = D1Client(**get_d1_config())
    cutoff = (datetime.now(UTC) - timedelta(hours=FINAL_RECHECK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    games = client.query(_GAMES_BEHIND_SQL, [cutoff]).results
    if not games:
        return
    _load_plays_for_games(games, client)


def backfill_week_scoring_plays(week_number: int) -> None:
    """Every game in a week, regardless of the score check - for weeks that
    finished before this loader existed."""
    client = D1Client(**get_d1_config())
    games = client.query(_WEEK_GAMES_SQL, [SEASON, week_number]).results
    if not games:
        logger.warning("No games for season %s week %s", SEASON, week_number)
        return
    _load_plays_for_games(games, client)


def main() -> None:
    """scoring plays - a week number backfills that week"""
    if len(sys.argv) > 2:
        backfill_week_scoring_plays(int(sys.argv[2]))
    else:
        load_scoring_plays()


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    main()
