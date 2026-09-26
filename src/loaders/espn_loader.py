"""Sync per-game ESPN data that's known before kickoff onto games -
currently neutral_site (plus linking espn_event_id up front, so
game_snapshots_loader.py can join directly instead of matching by team
abbreviation on a game's first live tick).

neutral_site comes straight from ESPN's competitions[].neutralSite - covers
international games and any domestic neutral-site game alike. Confirmed
live 2026-09-26: all 9 of this season's neutralSite games are exactly the
9 games.is_international games, with the same home/away designation as
Sports IO.

ESPN is a best-effort source (see api/CLAUDE.md) - a failed week fetch is
logged and skipped, never raised, so housekeeping keeps going.

Usage: uv run python -m src.loaders.espn_loader [local|prod]
(the CLI form syncs every week of the season, not just incomplete ones)
"""

import logging
import sys
from typing import Any

from api.espn_client import ABBREV_CORRECTIONS as ESPN_ABBREV_CORRECTIONS
from api.espn_client import get_scoreboard
from config.config import SEASON, configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.loaders.loader_helper import mapping_gap_statement, sql_batch_call

logger = logging.getLogger(__name__)

_WEEK_GAMES_SQL = """
SELECT w.week_number, g.game_id, ht.abbreviation AS home_abbrev, at.abbreviation AS away_abbrev
FROM games g
JOIN weeks w ON w.week_id = g.week_id
JOIN teams ht ON ht.team_id = g.home_team_id
JOIN teams at ON at.team_id = g.away_team_id
WHERE w.season_id = ?
"""

_UPDATE_GAME_SQL = "UPDATE games SET espn_event_id = ?, neutral_site = ? WHERE game_id = ?"


def load_espn_games(include_complete: bool = False) -> None:
    """set neutral_site/espn_event_id for every game in this season's
    incomplete weeks (or every week, with include_complete) - one ESPN call
    per week. Completed weeks are skipped by default since neither value
    changes once a game has been played."""
    client = D1Client(**get_d1_config())

    sql = _WEEK_GAMES_SQL if include_complete else _WEEK_GAMES_SQL + " AND w.is_complete = 0"
    games_by_week: dict[int, dict[tuple[str, str], int]] = {}
    for row in client.query(sql, [SEASON]).results:
        games_by_week.setdefault(row["week_number"], {})[
            (row["home_abbrev"], row["away_abbrev"])
        ] = row["game_id"]
    if not games_by_week:
        logger.info("No games to sync from ESPN")
        return

    statements: list[tuple[str, list[Any] | None]] = []
    gap_statements: list[tuple[str, list[Any] | None]] = []
    for week_number, game_ids in sorted(games_by_week.items()):
        try:
            scoreboard = get_scoreboard(week_number)
        except Exception:
            logger.exception("ESPN scoreboard fetch failed for week %s - skipping", week_number)
            continue

        for event in scoreboard.events:
            competition = event.competitions[0]
            by_side = {c.home_away: c.team.abbreviation for c in competition.competitors}
            home = ESPN_ABBREV_CORRECTIONS.get(by_side.get("home", ""), by_side.get("home"))
            away = ESPN_ABBREV_CORRECTIONS.get(by_side.get("away", ""), by_side.get("away"))
            game_id = game_ids.get((home, away))
            if game_id is None:
                logger.warning(
                    "No games row for ESPN event %s (%s @ %s, week %s)",
                    event.id,
                    away,
                    home,
                    week_number,
                )
                gap_statements.append(
                    mapping_gap_statement("espn", "team_pair", f"{away}@{home}", "load_espn_games")
                )
                continue
            statements.append((_UPDATE_GAME_SQL, [event.id, competition.neutral_site, game_id]))

    if statements or gap_statements:
        sql_batch_call(statements + gap_statements, client)
    logger.info("Synced %d game(s) from ESPN", len(statements))


def main() -> None:
    """every week of the season - use for a first run/backfill"""
    load_espn_games(include_complete=True)


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    main()
