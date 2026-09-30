"""
Load per-player box scores (Sports IO /games/statistics/players) into
game_player_stats.

Polled every minute during live games, so rather than rewrite every row
each time it diffs against what's stored and only upserts the rows whose
stats actually changed (a handful per game per minute) and deletes rows
Sports IO no longer returns (a stat correction that removes a player's
line entirely).

Usage:
  uv run python -m src.loaders.player_stats_loader [local|prod]
    (currently-live games)
  uv run python -m src.loaders.player_stats_loader [local|prod] <week_number>
    (every game in that week - backfill, or the FINAL capture)
"""

import json
import logging
import re
import sys
from typing import Any

from api.sports_io_client import get_player_statistics
from api.sports_io_models import PlayerStat
from config.config import SEASON, configure_logging, load_env
from db.clients import get_d1
from db.d1_client import D1Client
from src.game_rules import LIVE_STATUSES, sql_list
from src.loaders.loader_helper import id_map, mapping_gap_statement, sql_batch_call

logger = logging.getLogger(__name__)

_LIVE_GAMES_SQL = f"""
SELECT game_id, sports_io_game_id FROM games
WHERE status IN {sql_list(LIVE_STATUSES)} AND sports_io_game_id IS NOT NULL
"""

_WEEK_GAMES_SQL = """
SELECT g.game_id, g.sports_io_game_id
FROM games g
JOIN weeks w ON w.week_id = g.week_id
WHERE w.season_id = ? AND w.week_number = ? AND g.sports_io_game_id IS NOT NULL
"""

# {} is filled with one ? per game_id
_STORED_ROWS_SQL = """
SELECT game_id, team_id, stat_group, player_name, sports_io_player_id,
    player_image, stats_json
FROM game_player_stats
WHERE game_id IN ({})
"""

_UPSERT_SQL = """
INSERT INTO game_player_stats (
    game_id, team_id, stat_group, player_name, sports_io_player_id,
    player_image, stats_json
)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(game_id, team_id, stat_group, player_name) DO UPDATE SET
    sports_io_player_id = excluded.sports_io_player_id,
    player_image = excluded.player_image,
    stats_json = excluded.stats_json,
    updated_at = CURRENT_TIMESTAMP
"""

_DELETE_SQL = """
DELETE FROM game_player_stats
WHERE game_id = ? AND team_id = ? AND stat_group = ? AND player_name = ?
"""

_INT_RE = re.compile(r"-?\d+")
_FLOAT_RE = re.compile(r"-?\d+\.\d+")

type RowKey = tuple[int, int, str, str]  # (game_id, team_id, stat_group, player_name)


def _stat_key(name: str) -> str:
    """'passing touch downs' -> 'passing_touch_downs'"""
    return "_".join(name.lower().split())


def _stat_value(value: str | None) -> int | float | str | None:
    """numbers as numbers; compound values ('19/34' comp/att, '2-19'
    sacks-yards) stay strings, same as Sports IO sends them"""
    if value is None:
        return None
    if _INT_RE.fullmatch(value):
        return int(value)
    if _FLOAT_RE.fullmatch(value):
        return float(value)
    return value


def _stats_json(statistics: list[PlayerStat]) -> str:
    return json.dumps(
        {_stat_key(stat.name): _stat_value(stat.value) for stat in statistics},
        separators=(",", ":"),
    )


def _load_for_games(games: list[dict[str, Any]], client: D1Client) -> set[int]:
    """Diff each game's Sports IO player stats against game_player_stats
    and write only the changes. Returns the game_ids that changed, for the
    caller's KV write. A failed fetch for one game is logged and skipped."""
    if not games:
        return set()

    team_ids = id_map(client, "teams", "sports_io_team_id", "team_id")
    game_ids = [game["game_id"] for game in games]
    stored: dict[RowKey, tuple[Any, Any, str]] = {
        (row["game_id"], row["team_id"], row["stat_group"], row["player_name"]): (
            row["sports_io_player_id"],
            row["player_image"],
            row["stats_json"],
        )
        for row in client.query(
            _STORED_ROWS_SQL.format(", ".join("?" * len(game_ids))), game_ids
        ).results
    }

    gap_statements: list[tuple[str, list[Any] | None]] = []
    changed: set[int] = set()
    row_changes = 0
    for game in games:
        game_id = game["game_id"]
        # one batch per game - a whole week at once is ~1,300 statements
        statements: list[tuple[str, list[Any] | None]] = []
        try:
            teams = get_player_statistics(game["sports_io_game_id"])
        except Exception:
            logger.exception("Player stats fetch failed for game_id=%s - skipping", game_id)
            continue
        if not teams:
            # not started yet (or Sports IO hiccup) - never wipe stored rows
            continue

        fetched: set[RowKey] = set()
        for team in teams:
            team_id = team_ids.get(team.team.id)
            if team_id is None:
                logger.warning(
                    "No teams row for sports_io team %s (game_id=%s)",
                    team.team.id,
                    game_id,
                )
                gap_statements.append(
                    mapping_gap_statement(
                        "sports_io", "team", team.team.id, "load_player_stats"
                    )
                )
                continue

            for group in team.groups:
                for line in group.players:
                    if not line.player.name:
                        continue
                    key = (game_id, team_id, group.name, line.player.name)
                    fetched.add(key)
                    values = (
                        line.player.id,
                        line.player.image,
                        _stats_json(line.statistics),
                    )
                    if stored.get(key) != values:
                        statements.append((_UPSERT_SQL, [*key, *values]))
                        changed.add(game_id)

        # only for teams we could map - an unmapped team's stored rows
        # (there shouldn't be any) are left alone rather than deleted
        mapped_teams = {team_ids.get(team.team.id) for team in teams}
        for key in stored:
            if key[0] == game_id and key[1] in mapped_teams and key not in fetched:
                statements.append((_DELETE_SQL, list(key)))
                changed.add(game_id)

        if statements:
            sql_batch_call(statements, client)
            row_changes += len(statements)

    if gap_statements:
        sql_batch_call(gap_statements, client)
    logger.info(
        "Player stats: %d row change(s) across %d of %d game(s)",
        row_changes,
        len(changed),
        len(games),
    )
    return changed


def load_live_player_stats() -> set[int]:
    """currently-live games - returns the game_ids whose stats changed"""
    client = get_d1()
    return _load_for_games(client.query(_LIVE_GAMES_SQL).results, client)


def load_week_player_stats(week_number: int) -> set[int]:
    """every game in a week - returns the game_ids whose stats changed"""
    client = get_d1()
    games = client.query(_WEEK_GAMES_SQL, [SEASON, week_number]).results
    if not games:
        logger.warning("No games for season %s week %s", SEASON, week_number)
    return _load_for_games(games, client)


def main() -> None:
    """player stats - a week number loads that whole week"""
    if len(sys.argv) > 2:
        load_week_player_stats(int(sys.argv[2]))
    else:
        load_live_player_stats()


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    main()
