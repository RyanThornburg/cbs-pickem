"""Load Sports IO data

Usage: uv run python -m src.loaders.sports_io_loader [local|prod]

"""

import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from api.sports_io_client import get_games, get_live_games
from api.sports_io_models import Game
from config.config import SEASON, configure_logging, get_d1_config, load_env
from db.d1_client import D1Client, D1Error

logger = logging.getLogger(__name__)

_UPSERT_WEEK_SQL = """
INSERT INTO weeks (season_id, week_number, name, start_time, end_time)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(season_id, week_number) DO UPDATE SET
    name = excluded.name,
    start_time = excluded.start_time,
    end_time = excluded.end_time
"""

_UPSERT_GAMES_SQL = """
INSERT INTO games (
    week_id, home_team_id, away_team_id, sports_io_game_id,
    game_time, home_score, away_score, status, status_desc)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(sports_io_game_id) DO UPDATE SET
    week_id = excluded.week_id,
    home_team_id = excluded.home_team_id,
    away_team_id = excluded.away_team_id,
    game_time = excluded.game_time,
    home_score = excluded.home_score,
    away_score = excluded.away_score,
    status = excluded.status,
    status_desc = excluded.status_desc
ON CONFLICT(week_id, home_team_id, away_team_id) DO UPDATE SET
    sports_io_game_id = excluded.sports_io_game_id,
    game_time = excluded.game_time,
    home_score = excluded.home_score,
    away_score = excluded.away_score,
    status = excluded.status,
    status_desc = excluded.status_desc
"""

_SPORTS_IO_STATUS_MAP = {
    "NS": "SCHEDULED",
    "Q1": "IN_PROGRESS",
    "Q2": "IN_PROGRESS",
    "Q3": "IN_PROGRESS",
    "Q4": "IN_PROGRESS",
    "OT": "IN_PROGRESS",
    "HT": "HALFTIME",
    "FT": "FINAL",
    "AOT": "FINAL",
    "CANC": "CANCELLED",
    "PST": "POSTPONED",
}


def _sports_io_status_to_common(short_status: str) -> str:
    """Map Sports IO's game.status.short onto the common games.status vocabulary."""
    status = _SPORTS_IO_STATUS_MAP.get(short_status)
    if status is None:
        logger.warning(
            "Unrecognized Sports IO game status %r - leaving unmapped", short_status
        )
        return short_status
    return status


_REGULAR_SEASON_WEEK_RE = re.compile(r"^Week (\d+)$")


# TODO: if pre/post season is needed, fix
def _regular_season_week_number(week_name: str) -> int | None:
    """Pick Em is regular season only so parsing Week N to n.
    Need to update if preseason or post season is needed"""
    match = _REGULAR_SEASON_WEEK_RE.match(week_name)
    return int(match.group(1)) if match else None


def _epoch_seconds_to_iso(timestamp: int) -> str:
    """game.game.date.timestamp is epoch seconds. Convert to ISO8601 UTC string"""
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sql_batch_call(statements: list[tuple[str, list[Any] | None]]):
    client = D1Client(**get_d1_config())
    try:
        client.batch(statements)
    except D1Error:
        logger.exception("Loading data failed")
        sys.exit(1)


def _sports_io_id_map(
    client: D1Client, table: str, sportsio_column: str, pk_column: str
) -> dict[Any, int]:
    """sports io external id : internal id for each row in the table"""
    result = client.query(
        f"SELECT {pk_column}, {sportsio_column} FROM {table} WHERE {sportsio_column} IS NOT NULL"
    )
    return {row[sportsio_column]: row[pk_column] for row in result.results}


# TODO: allow date as a param?
def load_games_data(env: str = "local", live: bool = False) -> None:
    """load games from sports io"""
    if not load_env(env):
        sys.exit(1)

    client = D1Client(**get_d1_config())

    games: list[Game] = get_games() if not live else get_live_games()

    # don't care about preseason data and could run into Week n issues between
    # preseason and regular season if we don't filter out pre season here
    regular_and_post_games = [g for g in games if g.game.stage != "Pre Season"]
    if len(regular_and_post_games) != len(games):
        logger.info(
            "Skipped %d preseason games", len(games) - len(regular_and_post_games)
        )

    week_info: dict[int, tuple[str, int, int]] = {}
    for game in regular_and_post_games:
        week_number = _regular_season_week_number(game.game.week)
        if week_number is None:
            continue
        timestamp = game.game.date.timestamp
        if week_number not in week_info:
            week_info[week_number] = (game.game.week, timestamp, timestamp)
        else:
            name, min_ts, max_ts = week_info[week_number]
            week_info[week_number] = (
                name,
                min(min_ts, timestamp),
                max(max_ts, timestamp),
            )

    if week_info:
        _sql_batch_call(
            [
                (
                    _UPSERT_WEEK_SQL,
                    [
                        SEASON,
                        week_number,
                        name,
                        _epoch_seconds_to_iso(min_ts),
                        _epoch_seconds_to_iso(max_ts),
                    ],
                )
                for week_number, (name, min_ts, max_ts) in week_info.items()
            ]
        )

    team_ids = _sports_io_id_map(client, "teams", "sports_io_team_id", "team_id")
    week_ids = _sports_io_id_map(client, "weeks", "name", "week_id")

    statements: list[tuple[str, list[Any] | None]] = []
    for game in regular_and_post_games:
        week_id = week_ids.get(game.game.week)
        home_team_id = team_ids.get(game.teams.home.id)
        away_team_id = team_ids.get(game.teams.away.id)
        if week_id is None or home_team_id is None or away_team_id is None:
            logger.warning(
                "Skipping game %s - no weeks/teams row for week=%s home=%s away=%s yet",
                game.game.id,
                game.game.week,
                game.teams.home.id,
                game.teams.away.id,
            )
            continue

        statements.append(
            (
                _UPSERT_GAMES_SQL,
                [
                    week_id,
                    home_team_id,
                    away_team_id,
                    game.game.id,
                    _epoch_seconds_to_iso(game.game.date.timestamp),
                    game.scores.home.total,
                    game.scores.away.total,
                    _sports_io_status_to_common(game.game.status.short),
                    game.game.status.long,
                ],
            )
        )

    if statements:
        _sql_batch_call(statements)
        logger.info("Upserted %d games into D1 (%s)", len(statements), env)


def main(env: str = "local") -> None:
    """load game data"""
    if not load_env(env):
        sys.exit(1)

    load_games_data(env)


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
