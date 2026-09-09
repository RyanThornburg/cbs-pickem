"""Load Sports IO data

Usage: uv run python -m src.loaders.sports_io_loader [local|prod]

"""

import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from api.sports_io_client import get_games, get_live_games, get_team_statistics
from api.sports_io_models import Game, TeamStatistics
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
    game_time, home_score, away_score, status, status_desc,
    stadium_id, is_international)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(sports_io_game_id) DO UPDATE SET
    week_id = excluded.week_id,
    home_team_id = excluded.home_team_id,
    away_team_id = excluded.away_team_id,
    game_time = excluded.game_time,
    home_score = excluded.home_score,
    away_score = excluded.away_score,
    status = excluded.status,
    status_desc = excluded.status_desc,
    stadium_id = excluded.stadium_id,
    is_international = excluded.is_international
ON CONFLICT(week_id, home_team_id, away_team_id) DO UPDATE SET
    sports_io_game_id = excluded.sports_io_game_id,
    game_time = excluded.game_time,
    home_score = excluded.home_score,
    away_score = excluded.away_score,
    status = excluded.status,
    status_desc = excluded.status_desc,
    stadium_id = excluded.stadium_id,
    is_international = excluded.is_international
"""

_UPSERT_GAME_TEAM_STATS_SQL = """
INSERT INTO game_team_stats (
    game_id, team_id,
    first_downs_total, first_downs_passing, first_downs_rushing, first_downs_penalties,
    third_down_conversions, third_down_attempts, fourth_down_conversions, fourth_down_attempts,
    plays_total, yards_total, yards_per_play, total_drives,
    passing_yards, passing_completions, passing_attempts, yards_per_pass,
    interceptions_thrown, sacks_given_up, sack_yards_lost,
    rushing_yards, rushing_attempts, yards_per_rush,
    redzone_made, redzone_attempts,
    penalties, penalty_yards,
    total_turnovers, fumbles_lost,
    interceptions, fumbles_recovered, sacks_recorded, int_touchdowns,
    safeties, points_against, time_of_possession_sec
)
VALUES (
    ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?,
    ?, ?,
    ?, ?,
    ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?
)
ON CONFLICT(game_id, team_id) DO UPDATE SET
    first_downs_total = excluded.first_downs_total,
    first_downs_passing = excluded.first_downs_passing,
    first_downs_rushing = excluded.first_downs_rushing,
    first_downs_penalties = excluded.first_downs_penalties,
    third_down_conversions = excluded.third_down_conversions,
    third_down_attempts = excluded.third_down_attempts,
    fourth_down_conversions = excluded.fourth_down_conversions,
    fourth_down_attempts = excluded.fourth_down_attempts,
    plays_total = excluded.plays_total,
    yards_total = excluded.yards_total,
    yards_per_play = excluded.yards_per_play,
    total_drives = excluded.total_drives,
    passing_yards = excluded.passing_yards,
    passing_completions = excluded.passing_completions,
    passing_attempts = excluded.passing_attempts,
    yards_per_pass = excluded.yards_per_pass,
    interceptions_thrown = excluded.interceptions_thrown,
    sacks_given_up = excluded.sacks_given_up,
    sack_yards_lost = excluded.sack_yards_lost,
    rushing_yards = excluded.rushing_yards,
    rushing_attempts = excluded.rushing_attempts,
    yards_per_rush = excluded.yards_per_rush,
    redzone_made = excluded.redzone_made,
    redzone_attempts = excluded.redzone_attempts,
    penalties = excluded.penalties,
    penalty_yards = excluded.penalty_yards,
    total_turnovers = excluded.total_turnovers,
    fumbles_lost = excluded.fumbles_lost,
    interceptions = excluded.interceptions,
    fumbles_recovered = excluded.fumbles_recovered,
    sacks_recorded = excluded.sacks_recorded,
    int_touchdowns = excluded.int_touchdowns,
    safeties = excluded.safeties,
    points_against = excluded.points_against,
    time_of_possession_sec = excluded.time_of_possession_sec
"""

# Sports IO's game.venue.name uses stale/former stadium names in at least
# one confirmed case - maps those onto stadiums_loader.py's current/real
# name instead of matching games onto a stale name stored in the table.
VENUE_NAME_CORRECTIONS = {
    "Reliant Stadium": "NRG Stadium",  # Texans' stadium, renamed 2010
    "FC Bayern Munich Stadium": "Allianz Arena",  # Munich, Germany
}


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


def _parse_made_attempted(value: str, sep: str = "-") -> tuple[int, int]:
    """Parse Sports IO's "N<sep>M" stat strings (comp_att uses "/", everything
    else - sacks_yards_lost, made_att, third/fourth_down_efficiency,
    penalties.total - uses "-") into (made, attempted) ints."""
    made, attempted = value.split(sep)
    return int(made), int(attempted)


def _parse_time_of_possession(value: str) -> int:
    """Parse posession.total's "MM:SS" into total seconds."""
    minutes, seconds = value.split(":")
    return int(minutes) * 60 + int(seconds)


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
    stadiums = {
        row["name"]: (row["stadium_id"], row["country"] != "USA")
        for row in client.query("SELECT stadium_id, name, country FROM stadiums").results
    }

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

        # venue is None for undetermined future playoff matchups; a venue
        # name not in `stadiums` (e.g. a new international site next
        # season) just leaves stadium_id unset rather than skipping the
        # game - stadium_id is nullable, unlike week/team.
        stadium_id, is_international = (None, False)
        venue_name = game.game.venue.name if game.game.venue else None
        if venue_name is not None:
            venue_name = VENUE_NAME_CORRECTIONS.get(venue_name, venue_name)
            match = stadiums.get(venue_name)
            if match is None:
                logger.warning(
                    "No stadiums row for venue %r (game %s) - leaving stadium_id unset",
                    venue_name,
                    game.game.id,
                )
            else:
                stadium_id, is_international = match

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
                    stadium_id,
                    is_international,
                ],
            )
        )

    if statements:
        _sql_batch_call(statements)
        logger.info("Upserted %d games into D1 (%s)", len(statements), env)


def _fetch_week_game_id_map(week_id: int, client: D1Client) -> dict[int, int]:
    """sports_io_game_id : internal game_id, for games in this week"""
    result = client.query(
        "SELECT game_id, sports_io_game_id FROM games "
        "WHERE week_id = ? AND sports_io_game_id IS NOT NULL",
        [week_id],
    )
    return {row["sports_io_game_id"]: row["game_id"] for row in result.results}


def _game_team_stats_statement(
    game_id: int, team_id: int, team_stats: TeamStatistics
) -> tuple[str, list[Any] | None]:
    stats = team_stats.statistics
    third_down_conversions, third_down_attempts = _parse_made_attempted(
        stats.first_downs.third_down_efficiency
    )
    fourth_down_conversions, fourth_down_attempts = _parse_made_attempted(
        stats.first_downs.fourth_down_efficiency
    )
    passing_completions, passing_attempts = _parse_made_attempted(
        stats.passing.comp_att, sep="/"
    )
    sacks_given_up, sack_yards_lost = _parse_made_attempted(stats.passing.sacks_yards_lost)
    redzone_made, redzone_attempts = _parse_made_attempted(stats.red_zone.made_att)
    penalties, penalty_yards = _parse_made_attempted(stats.penalties.total)

    return (
        _UPSERT_GAME_TEAM_STATS_SQL,
        [
            game_id,
            team_id,
            stats.first_downs.total,
            stats.first_downs.passing,
            stats.first_downs.rushing,
            stats.first_downs.from_penalties,
            third_down_conversions,
            third_down_attempts,
            fourth_down_conversions,
            fourth_down_attempts,
            stats.plays.total,
            stats.yards.total,
            float(stats.yards.yards_per_play),
            int(stats.yards.total_drives),
            stats.passing.total,
            passing_completions,
            passing_attempts,
            float(stats.passing.yards_per_pass),
            stats.passing.interceptions_thrown,
            sacks_given_up,
            sack_yards_lost,
            stats.rushings.total,
            stats.rushings.attempts,
            float(stats.rushings.yards_per_rush),
            redzone_made,
            redzone_attempts,
            penalties,
            penalty_yards,
            stats.turnovers.total,
            stats.turnovers.lost_fumbles,
            stats.interceptions.total,
            stats.fumbles_recovered.total,
            stats.sacks.total,
            stats.int_touchdowns.total,
            stats.safeties.total,
            stats.points_against.total,
            _parse_time_of_possession(stats.posession.total),
        ],
    )


def load_game_statistics(week: int, env: str = "local") -> None:
    """load per-team box score stats for every game in a week"""
    if not load_env(env):
        sys.exit(1)

    client = D1Client(**get_d1_config())

    team_ids = _sports_io_id_map(client, "teams", "sports_io_team_id", "team_id")

    week_row = client.query(
        "SELECT week_id FROM weeks WHERE season_id = ? AND week_number = ?",
        [SEASON, week],
    ).results
    if not week_row:
        logger.warning("No weeks row for season=%s week=%s", SEASON, week)
        return
    week_id = week_row[0]["week_id"]

    game_ids = _fetch_week_game_id_map(week_id, client)
    if not game_ids:
        logger.warning("No games with a sports_io_game_id for week %s yet", week)
        return

    statements: list[tuple[str, list[Any] | None]] = []
    for sports_io_game_id, game_id in game_ids.items():
        for team_stats in get_team_statistics(sports_io_game_id):
            team_id = team_ids.get(team_stats.team.id)
            if team_id is None:
                logger.warning(
                    "Skipping stats for sports_io team %s in game %s - no teams row yet",
                    team_stats.team.id,
                    sports_io_game_id,
                )
                continue

            statements.append(_game_team_stats_statement(game_id, team_id, team_stats))

    if not statements:
        logger.warning("No game stats to load for week %s", week)
        return

    _sql_batch_call(statements)
    logger.info(
        "Upserted %d game_team_stats rows for week %s (%s)", len(statements), week, env
    )


def main(env: str = "local") -> None:
    """load game data"""
    if not load_env(env):
        sys.exit(1)

    load_games_data(env)


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
