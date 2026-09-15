"""Load CBS data

Usage: uv run python -m src.loaders.cbs_loader [local|prod]
Backfill a past week: uv run python -m src.loaders.cbs_loader [local|prod] <week_number>
"""

import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from api.cbs_client import (
    ABBREV_CORRECTIONS,
    get_cbs_pool_home,
    get_cbs_pool_teams,
    get_cbs_users,
    get_cbs_weekly,
)
from api.cbs_models import (
    FootballPickemManagerPool,
    FootballPickemPoolHome,
    FootballPickemWeeklyStandingsEntry,
    FootballPickemWeeklyStandingsPick,
    Member,
    PoolEvent,
)
from config.config import SEASON, configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.loaders.loader_helper import id_map, mapping_gap_statement, sql_batch_call

logger = logging.getLogger(__name__)

_UPSERT_USERS_SQL = """
INSERT INTO users (name, email, cbs_id, is_active)
VALUES (?, ?, ?, ?)
ON CONFLICT(cbs_id) DO UPDATE SET
    name = excluded.name,
    email = excluded.email,
    cbs_id = excluded.cbs_id,
    is_active = excluded.is_active
"""
_UPSERT_USERS_PICK = """
INSERT INTO user_picks (user_id, game_id, picked_team_id, is_correct, trending_status, cbs_pick_id)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id, game_id) DO UPDATE SET
    picked_team_id = excluded.picked_team_id,
    is_correct = excluded.is_correct,
    trending_status = excluded.trending_status,
    cbs_pick_id = excluded.cbs_pick_id
"""
_UPSERT_USER_WEEKLY = """
INSERT INTO weekly_performance (user_id, week_id, has_submitted_picks, picks_made, picks_correct, trending_score)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id, week_id) DO UPDATE SET
    has_submitted_picks = excluded.has_submitted_picks,
    picks_made = excluded.picks_made,
    picks_correct = excluded.picks_correct,
    trending_score = excluded.trending_score
"""

_UPSERT_GAMES_SQL = """
INSERT INTO games (week_id, home_team_id, away_team_id, cbs_event_id, game_time, cbs_spread,
    home_score, away_score, status, tv_network, gametracker_url, status_desc)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(cbs_event_id) DO UPDATE SET
    week_id = excluded.week_id,
    home_team_id = excluded.home_team_id,
    away_team_id = excluded.away_team_id,
    game_time = excluded.game_time,
    cbs_spread = excluded.cbs_spread,
    home_score = excluded.home_score,
    away_score = excluded.away_score,
    status = excluded.status,
    tv_network = excluded.tv_network,
    gametracker_url = excluded.gametracker_url,
    status_desc = excluded.status_desc
ON CONFLICT(week_id, home_team_id, away_team_id) DO UPDATE SET
    cbs_event_id = excluded.cbs_event_id,
    game_time = excluded.game_time,
    cbs_spread = excluded.cbs_spread,
    home_score = excluded.home_score,
    away_score = excluded.away_score,
    status = excluded.status,
    tv_network = excluded.tv_network,
    gametracker_url = excluded.gametracker_url,
    status_desc = excluded.status_desc
"""
_UPDATE_CBS_TEAM_SQL = """
UPDATE teams SET
    cbs_team_id = ?,
    medium_name = ?,
    nick_name = ?,
    color_primary_hex = ?,
    color_secondary_hex = ?
WHERE abbreviation = ?
"""

# CBS's pool home page is the only source for the real name
_UPDATE_SEASON_NAME_SQL = "UPDATE seasons SET name = ? WHERE season_id = ?"

_UPSERT_WEEK_SQL = """
INSERT INTO weeks (season_id, week_number, name, cbs_pool_period_id, is_current)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(cbs_pool_period_id) DO UPDATE SET
    season_id = excluded.season_id,
    week_number = excluded.week_number,
    name = excluded.name,
    is_current = excluded.is_current
ON CONFLICT(season_id, week_number) DO UPDATE SET
    name = excluded.name,
    cbs_pool_period_id = excluded.cbs_pool_period_id,
    is_current = excluded.is_current
"""

_CBS_STATUS_MAP = {
    "SCHEDULED": "SCHEDULED",
    "INPROGRESS": "IN_PROGRESS",
    "HALFTIME": "HALFTIME",
    "FINAL": "FINAL",
    "POSTPONED": "POSTPONED",
    "CANCELLED": "CANCELLED",
}


def _cbs_status_to_common(status_desc: str) -> str:
    """Map CBS's game_status_desc onto the common games.status vocabulary."""
    status_desc = status_desc.upper()
    status = _CBS_STATUS_MAP.get(status_desc)
    if status is None:
        logger.warning(
            "Unrecognized CBS game status %r - leaving unmapped", status_desc
        )
        return status_desc
    return status


def _cbs_starts_at_to_iso(starts_at_millis: int) -> str:
    """CBS's game.starts_at is epoch millis - convert to an ISO8601 UTC
    string for a consistent format"""
    return datetime.fromtimestamp(starts_at_millis / 1000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def load_cbs_users() -> None:
    """load users table from cbs data"""
    users: list[Member] = get_cbs_users()

    statements: list[tuple[str, list[Any] | None]] = [
        (_UPSERT_USERS_SQL, [user.name, user.email, user.id, True]) for user in users
    ]

    if not statements:
        logger.warning("No Users to load")
        return
    sql_batch_call(statements)


def map_cbs_to_sports_io():
    """
    Match CBS teams to existing team row from Sports IO
    Match on abbreviation and add CBS-only fields.
    """
    cbs_teams = get_cbs_pool_teams()
    statements: list[tuple[str, list[Any] | None]] = [
        (
            _UPDATE_CBS_TEAM_SQL,
            [
                team.cbs_team_id,
                team.medium_name,
                team.nick_name,
                team.color_primary_hex,
                team.color_secondary_hex,
                ABBREV_CORRECTIONS.get(team.abbrev, team.abbrev),
            ],
        )
        for team in cbs_teams
    ]

    if not statements:
        logger.warning("No CBS teams to map")
        return

    sql_batch_call(statements)
    logger.info("Mapped %d CBS teams onto teams table", len(statements))


def load_cbs_weeks() -> None:
    """update data from weekly cbs feed"""
    data = get_cbs_pool_home()
    if data is None:
        return

    statements: list[tuple[str, list[Any] | None]] = [
        (
            _UPSERT_WEEK_SQL,
            [SEASON, period.order, period.description, period.id, period.is_current],
        )
        for period in data.pool_periods
    ]

    if not statements:
        logger.warning("No pool periods to load")
        return

    week_count = len(statements)
    statements.append((_UPDATE_SEASON_NAME_SQL, [data.name, SEASON]))
    sql_batch_call(statements)
    logger.info("Upserted %d weeks into D1", week_count)


# TODO: verify this is correct once data is live
def _pick_status_to_correct(pick_status: str) -> bool | None:
    """None unless a status is Correct/Incorrect"""
    if pick_status == "CORRECT":
        return True
    if pick_status == "INCORRECT":
        return False
    return None


def load_cbs_games(pool_period_id: str | None = None) -> None:
    """run at start of new week - or, with pool_period_id, backfill a past
    week (see backfill_cbs_week())"""
    client = D1Client(**get_d1_config())

    data: FootballPickemPoolHome | None = get_cbs_pool_home(pool_period_id)
    if data is None:
        return

    if not data.are_games_available:
        logger.warning("Weekly games aren't available yet")
        return

    week_ids = id_map(client, "weeks", "cbs_pool_period_id", "week_id")
    team_ids = id_map(client, "teams", "cbs_team_id", "team_id")

    pool_period_id = data.pool_period.id
    week_id = week_ids.get(pool_period_id)
    if week_id is None:
        logger.warning(
            "No weeks row for CBS pool period %r - has this week been seeded?",
            pool_period_id,
        )
        return

    games = data.pool_period.pool_events

    statements: list[tuple[str, list[Any] | None]] = []
    gap_statements: list[tuple[str, list[Any] | None]] = []
    for game in games:
        home_team_id = team_ids.get(game.home_team.cbs_team_id)
        away_team_id = team_ids.get(game.away_team.cbs_team_id)
        if home_team_id is None or away_team_id is None:
            logger.warning(
                "Skipping game %s - no teams row for cbs_team_id=%s/%s yet",
                game.cbs_event_id,
                game.home_team.cbs_team_id,
                game.away_team.cbs_team_id,
            )
            if home_team_id is None:
                gap_statements.append(
                    mapping_gap_statement(
                        "cbs", "team", game.home_team.cbs_team_id, "load_cbs_games"
                    )
                )
            if away_team_id is None:
                gap_statements.append(
                    mapping_gap_statement(
                        "cbs", "team", game.away_team.cbs_team_id, "load_cbs_games"
                    )
                )
            continue

        statements.append(
            (
                _UPSERT_GAMES_SQL,
                [
                    week_id,
                    home_team_id,
                    away_team_id,
                    game.cbs_event_id,
                    _cbs_starts_at_to_iso(game.starts_at),
                    game.home_team_spread,
                    game.home_team_score,
                    game.away_team_score,
                    _cbs_status_to_common(game.game_status_desc),
                    game.tv_info_name,
                    game.gametracker_link,
                    game.game_status_desc,
                ],
            )
        )

    if statements:
        sql_batch_call(statements + gap_statements, client)
        logger.info("Upserted %d games into D1", len(statements))
    elif gap_statements:
        sql_batch_call(gap_statements, client)


def _add_user_picks(
    user_id: int,
    picks: list[FootballPickemWeeklyStandingsPick],
    game_ids: dict[int, int],
    team_ids: dict[int, int],
    client: D1Client,
) -> None:
    statements: list[tuple[str, list[Any] | None]] = []
    gap_statements: list[tuple[str, list[Any] | None]] = []
    for pick in picks:
        assert pick.pick_info is not None  # filtered by the caller
        cbs_item_id = pick.pick_info.cbs_item_id
        game_id = game_ids.get(pick.cbs_slot_id)
        team_id = team_ids.get(cbs_item_id) if cbs_item_id is not None else None
        if game_id is None or team_id is None:
            logger.warning(
                "Skipping pick %r - no games/teams row for cbs_slot_id=%s / "
                "cbs_item_id=%s yet",
                pick.id,
                pick.cbs_slot_id,
                cbs_item_id,
            )
            # a null cbs_item_id just means this entry didn't pick this
            # game - not a real mapping gap, see api/CLAUDE.md
            if game_id is None:
                gap_statements.append(
                    mapping_gap_statement(
                        "cbs", "game", pick.cbs_slot_id, "_add_user_picks"
                    )
                )
            if team_id is None and cbs_item_id is not None:
                gap_statements.append(
                    mapping_gap_statement("cbs", "team", cbs_item_id, "_add_user_picks")
                )
            continue

        statements.append(
            (
                _UPSERT_USERS_PICK,
                [
                    user_id,
                    game_id,
                    team_id,
                    _pick_status_to_correct(pick.pick_info.pick_status),
                    pick.pick_info.trending_status,
                    pick.id,
                ],
            )
        )

    if statements or gap_statements:
        sql_batch_call(statements + gap_statements, client)


def load_cbs_user_picks(pool_period_id: str | None = None) -> None:
    """load users weekly picks - or, with pool_period_id, backfill a past
    week (see backfill_cbs_week())"""
    client = D1Client(**get_d1_config())

    data: FootballPickemManagerPool = get_cbs_weekly(pool_period_id)

    if data.standings is None or data.standings.weekly is None:
        logger.warning("No standings/picks data available yet")
        return

    user_ids = id_map(client, "users", "cbs_id", "user_id")
    week_ids = id_map(client, "weeks", "cbs_pool_period_id", "week_id")
    game_ids = id_map(client, "games", "cbs_event_id", "game_id")
    team_ids = id_map(client, "teams", "cbs_team_id", "team_id")

    pool_period_id = data.pool_period.id
    week_id = week_ids.get(pool_period_id)
    if week_id is None:
        logger.warning(
            "No weeks row for CBS pool period %r - has this week been seeded?",
            pool_period_id,
        )
        return

    # eligible games are locked, otherwise don't show a pick for that
    # would otherwise return my picks because I'm logged in
    # game events become locked after the start and the pick deadline
    game_events: Sequence[PoolEvent] = data.pool_period.pool_events
    locked_game_cbs_ids = [game.cbs_event_id for game in game_events if game.is_locked]

    entries: list[FootballPickemWeeklyStandingsEntry] = (
        data.standings.weekly.ranked_entries
    )

    weekly_statements: list[tuple[str, list[Any] | None]] = []
    gap_statements: list[tuple[str, list[Any] | None]] = []

    for entry in entries:
        member: Member = entry.entry.member
        user_id = user_ids.get(member.id)
        if user_id is None:
            logger.warning(
                "No users row for CBS member %r (%s) - skipping",
                member.id,
                member.name,
            )
            gap_statements.append(
                mapping_gap_statement("cbs", "user", member.id, "load_cbs_user_picks")
            )
            continue

        picks: list[FootballPickemWeeklyStandingsPick] = [
            pick
            for pick in entry.picks
            if pick.cbs_slot_id in locked_game_cbs_ids
            and pick.pick_info
            and pick.display_status != "LOCKED"
        ]

        weekly_statements.append(
            (
                _UPSERT_USER_WEEKLY,
                [
                    user_id,
                    week_id,
                    bool(entry.picks),
                    entry.score,
                    entry.period_score,
                    entry.trending_score,
                ],
            )
        )

        if picks:
            _add_user_picks(user_id, picks, game_ids, team_ids, client)

    if weekly_statements or gap_statements:
        sql_batch_call(weekly_statements + gap_statements, client)


def main() -> None:
    """load data from cbs"""
    load_cbs_weeks()
    load_cbs_games()
    load_cbs_user_picks()


_WEEK_POOL_PERIOD_ID_SQL = (
    "SELECT cbs_pool_period_id FROM weeks WHERE season_id = ? AND week_number = ?"
)


def backfill_cbs_week(week_number: int) -> None:
    """Backfill a past week's CBS-sourced data (games' cbs_event_id/
    cbs_spread, user_picks, weekly_performance) using its already-stored
    weeks.cbs_pool_period_id - confirmed live 2026-09-15 that CBS's
    weekly-standings/pool-home pages both accept a `poolPeriodId` query
    param to return a specific past period instead of always the current
    one. Requires that week to already have a row in `weeks` with
    cbs_pool_period_id set (load_cbs_weeks() populates it for every
    period on every run, current or not - not just the current week's)."""
    client = D1Client(**get_d1_config())
    row = client.query(_WEEK_POOL_PERIOD_ID_SQL, [SEASON, week_number]).results
    if not row or row[0]["cbs_pool_period_id"] is None:
        logger.warning(
            "No cbs_pool_period_id stored for season %s week %s - can't backfill",
            SEASON,
            week_number,
        )
        return

    pool_period_id = row[0]["cbs_pool_period_id"]
    load_cbs_games(pool_period_id)
    load_cbs_user_picks(pool_period_id)
    logger.info("Backfilled CBS data for season %s week %s", SEASON, week_number)


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    if len(sys.argv) > 2:
        backfill_cbs_week(int(sys.argv[2]))
    else:
        main()
