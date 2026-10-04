"""
Load scoring plays (ESPN's game summary) into game_scoring_plays.

Only fetches a game whose latest stored play doesn't match its current
score - the latest ESPN snapshot's, or games.home_score/away_score (Sports
IO's) for a game with no snapshots - i.e. someone scored since the last
fetch. That makes it cheap
enough to call every round: with nothing new it's a single D1 query, no
API calls. src/live_ticker.py calls it every 15 seconds right after the
snapshot capture, so a play lands within a round or two of the score;
orchestration calls it every minute too, for a game's last score landing
after it goes FINAL (the live ticker stops on FINAL).

Sports IO's /games/events was the source until 2026-10-04 - confirmed live
that day it ran ~5.5 minutes behind ESPN's score (its own live score lags
by about the same), while ESPN's summary had the play when its scoreboard
did. Both had identical plays for every FINAL 2026 game.

Usage:
  uv run python -m src.loaders.scoring_plays_loader [local|prod]
  uv run python -m src.loaders.scoring_plays_loader [local|prod] <week_number>
    (backfill every game in that week, regardless of the score check)
"""

import logging
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from api.espn_client import ABBREV_CORRECTIONS, get_summary
from api.espn_models import ScoringPlay
from config.config import SEASON, run_cli
from db.clients import get_d1
from db.d1_client import D1Client, Statement
from src.game_rules import DONE_STATUSES, LIVE_STATUSES, sql_list
from src.loaders.loader_helper import id_map, mapping_gap_statement, sql_batch_call
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

# a FINAL game is still checked for this long after kickoff, in case its
# last score landed after the game flipped to FINAL - also how far back a
# game ESPN has under way (Sports IO's status not caught up) is looked for
FINAL_RECHECK_HOURS = 6

# ESPN's play text leads with the scorer: 'Jonathan Taylor 5 Yd Rush
# (Spencer Shrader Kick)', 'Spencer Shrader 44 Yd Field Goal'. Matched 141
# of 151 plays to Sports IO's player name on weeks 3-4 - the rest were
# name style ('James Cook III' vs 'James Cook') plus one lateral play
# whose text has no 'Yd', left null.
_PLAYER_RE = re.compile(r"^(.+?) \d+ Yd\b")

# live (or recently FINAL) games, with what's stored for them and the
# score to compare against - the caller decides which are behind. Scores
# are 'home-away' strings, NULL when there's no score/play yet. The latest
# ESPN snapshot's score wins over the games row's, which is Sports IO's and
# minutes behind - comparing against both would refetch ESPN's ~800KB
# summary every round until Sports IO caught up.
_GAMES_SQL = f"""
SELECT g.game_id, g.espn_event_id,
    COALESCE(
        (
            SELECT s.home_score || '-' || s.away_score
            FROM game_snapshots s
            WHERE s.game_id = g.game_id
            ORDER BY s.snapshot_id DESC
            LIMIT 1
        ),
        g.home_score || '-' || g.away_score
    ) AS current_score,
    (
        SELECT p.home_score || '-' || p.away_score
        FROM game_scoring_plays p
        WHERE p.game_id = g.game_id
        ORDER BY p.sequence DESC
        LIMIT 1
    ) AS stored_score,
    (
        SELECT COUNT(*) FROM game_scoring_plays p WHERE p.game_id = g.game_id
    ) AS stored_plays
FROM games g
WHERE g.espn_event_id IS NOT NULL
  AND (
    g.status IN {sql_list(LIVE_STATUSES)}
    OR (g.status = 'FINAL' AND g.game_time >= ?)
    -- ESPN has it under way before Sports IO's status does (7+ minutes
    -- after kickoff on 2026-10-04) - a snapshot means it kicked off
    OR (
      (g.status IS NULL OR g.status NOT IN {sql_list(DONE_STATUSES)})
      AND g.game_time >= ?
      AND EXISTS (SELECT 1 FROM game_snapshots s WHERE s.game_id = g.game_id)
    )
  )
"""

# NULL stored_* so every game counts as changed and is rewritten
_WEEK_GAMES_SQL = """
SELECT g.game_id, g.espn_event_id, NULL AS stored_score, NULL AS stored_plays
FROM games g
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ? AND g.espn_event_id IS NOT NULL
"""

_DELETE_PLAYS_SQL = "DELETE FROM game_scoring_plays WHERE game_id = ?"

_INSERT_PLAY_SQL = """
INSERT INTO game_scoring_plays (
    game_id, sequence, quarter, clock, team_id, type, description,
    player_name, home_score, away_score
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _is_behind(game: dict[str, Any]) -> bool:
    """points on the board, and the latest stored play isn't at them"""
    score = game["current_score"]
    return score not in (None, "0-0") and score != game["stored_score"]


def _player_name(text: str) -> str | None:
    match = _PLAYER_RE.match(text.strip())
    return match.group(1) if match else None


def _play_statement(
    game_id: int, sequence: int, play: ScoringPlay, team_id: int | None
) -> Statement:
    return (
        _INSERT_PLAY_SQL,
        [
            game_id,
            sequence,
            # ESPN's period 5 is overtime, same scale as the column
            play.period.number if play.period else None,
            play.clock.display_value if play.clock else None,
            team_id,
            play.scoring_type.abbreviation if play.scoring_type else None,
            play.text.strip(),
            _player_name(play.text),
            play.home_score,
            play.away_score,
        ],
    )


def _load_plays_for_games(games: list[dict[str, Any]], client: D1Client) -> set[int]:
    """Replace each game's stored plays with ESPN's current list - returns
    the game_ids whose plays changed. A failed fetch for one game is
    logged and skipped, not fatal - scoring plays are enrichment, same as
    weather."""
    team_ids = id_map(client, "teams", "abbreviation", "team_id")

    statements: list[Statement] = []
    gap_statements: list[Statement] = []
    changed: set[int] = set()
    for game in games:
        try:
            plays = get_summary(game["espn_event_id"]).scoring_plays
        except Exception:
            logger.exception(
                "Scoring plays fetch failed for game_id=%s - skipping", game["game_id"]
            )
            continue

        if not plays:
            # leave whatever is stored rather than wiping it on a bad response
            continue

        last = f"{plays[-1].home_score}-{plays[-1].away_score}"
        if (len(plays), last) == (game["stored_plays"], game["stored_score"]):
            # ESPN's summary hasn't caught up to its scoreboard yet - normal
            # for a round or two, try again next round
            continue

        statements.append((_DELETE_PLAYS_SQL, [game["game_id"]]))
        for sequence, play in enumerate(plays, start=1):
            team_id = None
            if play.team is not None:
                abbrev = ABBREV_CORRECTIONS.get(
                    play.team.abbreviation, play.team.abbreviation
                )
                team_id = team_ids.get(abbrev)
                if team_id is None:
                    logger.warning(
                        "No teams row for ESPN team %s (game_id=%s)",
                        abbrev,
                        game["game_id"],
                    )
                    gap_statements.append(
                        mapping_gap_statement(
                            "espn", "team", abbrev, "load_scoring_plays"
                        )
                    )
            statements.append(_play_statement(game["game_id"], sequence, play, team_id))
        changed.add(game["game_id"])

    if statements or gap_statements:
        sql_batch_call(statements + gap_statements, client)
    if changed:
        logger.info(
            "Loaded scoring plays for %d of %d game(s)", len(changed), len(games)
        )
    return changed


def load_scoring_plays() -> set[int]:
    """Refresh scoring plays for every game whose score moved since the
    last fetch - see the module docstring. Returns the game_ids whose
    plays changed."""
    client = get_d1()
    cutoff = utc_iso(datetime.now(UTC) - timedelta(hours=FINAL_RECHECK_HOURS))
    rows = client.query(_GAMES_SQL, [cutoff, cutoff]).results
    games = [g for g in rows if _is_behind(g)]
    if not games:
        return set()
    return _load_plays_for_games(games, client)


def backfill_week_scoring_plays(week_number: int) -> set[int]:
    """Every game in a week, regardless of the score check - for weeks that
    finished before this loader existed, or to re-source them from ESPN."""
    client = get_d1()
    games = client.query(_WEEK_GAMES_SQL, [SEASON, week_number]).results
    if not games:
        logger.warning("No games for season %s week %s", SEASON, week_number)
        return set()
    return _load_plays_for_games(games, client)


def main() -> None:
    """scoring plays - a week number backfills that week"""
    if len(sys.argv) > 2:
        backfill_week_scoring_plays(int(sys.argv[2]))
    else:
        load_scoring_plays()


if __name__ == "__main__":
    run_cli(main)
