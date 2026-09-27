"""
Capture a snapshot (score/quarter/possession + weather) for every currently-live game
CBS's pool-home page for game state
Pirate Weather for conditions at the stadium's location.

Usage: uv run python -m src.loaders.game_snapshots_loader [local|prod]
"""

import logging
import sys
from datetime import UTC, datetime
from typing import Any

from api.cbs_client import get_cbs_pool_home
from api.espn_client import ABBREV_CORRECTIONS as ESPN_ABBREV_CORRECTIONS
from api.espn_client import get_scoreboard
from api.espn_models import Situation
from config.config import configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.loaders.loader_helper import capture_weather, mapping_gap_statement, sql_batch_call

logger = logging.getLogger(__name__)

_INSERT_SNAPSHOT_SQL = """
INSERT INTO game_snapshots (
    game_id, quarter, time_remaining, status_desc, possession, home_score, away_score,
    down, distance, yard_line, down_distance_text, possession_text,
    is_red_zone, home_timeouts, away_timeouts,
    last_play_text, last_play_type, drive_text, home_win_pct, away_win_pct,
    last_play_id,
    temperature_f, feels_like_f, weather_condition, weather_icon, precip_type,
    wind_speed_mph, wind_gust_mph, wind_direction, precipitation_pct,
    visibility_mi, weather_alerts_json, weather_captured_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?, ?, ?)
"""

# latest snapshot per live game - {} is filled with one ? per game_id
_LATEST_SNAPSHOT_SQL = """
SELECT * FROM game_snapshots
WHERE snapshot_id IN (
    SELECT MAX(snapshot_id) FROM game_snapshots WHERE game_id IN ({}) GROUP BY game_id
)
"""

# weather barely changes minute to minute - snapshots between refreshes
# carry the previous reading forward instead of spending a Pirate Weather call
WEATHER_REFRESH_SECONDS = 5 * 60

# game_snapshots' weather columns, in capture_weather()'s tuple order
_WEATHER_COLUMNS = (
    "temperature_f",
    "feels_like_f",
    "weather_condition",
    "weather_icon",
    "precip_type",
    "wind_speed_mph",
    "wind_gust_mph",
    "wind_direction",
    "precipitation_pct",
    "visibility_mi",
    "weather_alerts_json",
)

_NO_SITUATION = (None,) * 14


def _win_pct(fraction: float | None) -> float | None:
    return None if fraction is None else round(fraction * 100, 1)


def _situation_fields(situation: Situation | None) -> tuple[Any, ...]:
    """(down, distance, yard_line, down_distance_text, possession_text,
    is_red_zone, home_timeouts, away_timeouts, last_play_text,
    last_play_type, drive_text, home_win_pct, away_win_pct, last_play_id).
    None across the board if ESPN has no situation for this game right
    now - e.g. between plays like halftime, or if ESPN's
    abbreviation-matched event wasn't found."""
    if situation is None:
        return _NO_SITUATION

    last_play = situation.last_play
    probability = last_play.probability if last_play else None
    return (
        situation.down,
        situation.distance,
        situation.yard_line,
        situation.down_distance_text,
        situation.possession_text,
        situation.is_red_zone,
        situation.home_timeouts,
        situation.away_timeouts,
        # ESPN sometimes pads this with a leading space
        last_play.text.strip() if last_play and last_play.text else None,
        last_play.type.text if last_play and last_play.type else None,
        last_play.drive.description if last_play and last_play.drive else None,
        _win_pct(probability.home_win_percentage) if probability else None,
        _win_pct(probability.away_win_percentage) if probability else None,
        last_play.id if last_play else None,
    )


def _snapshot_weather(
    row: dict[str, Any], previous: dict[str, Any] | None
) -> tuple[Any, ...]:
    """capture_weather()'s tuple plus weather_captured_at. Reuses the
    previous snapshot's reading while it's under WEATHER_REFRESH_SECONDS
    old. weather_captured_at stays null for an enclosed roof or a failed
    fetch, so those retry on the next snapshot (enclosed never calls out)."""
    if previous is not None and previous["weather_captured_at"] is not None:
        captured = datetime.strptime(
            previous["weather_captured_at"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=UTC)
        if (datetime.now(UTC) - captured).total_seconds() < WEATHER_REFRESH_SECONDS:
            return (
                *(previous[column] for column in _WEATHER_COLUMNS),
                previous["weather_captured_at"],
            )

    weather = capture_weather(
        row["latitude"], row["longitude"], row["roof_type"], f"game_id={row['game_id']}"
    )
    captured_at = (
        datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        if any(value is not None for value in weather)
        else None
    )
    return (*weather, captured_at)


_UPDATE_GAME_ESPN_ID_SQL = "UPDATE games SET espn_event_id = ? WHERE game_id = ?"


def _fetch_espn_scoreboard_lookup() -> tuple[
    dict[str, Situation | None], dict[tuple[str, str], tuple[str, Situation | None]]
]:
    """two lookups - espn event id falls or home/away if event id not yet added
    espn is an undocumented/extra data source so not blocking on failures from it"""
    try:
        scoreboard = get_scoreboard()
    except Exception:
        logger.exception("ESPN scoreboard fetch failed - skipping situation data")
        return {}, {}

    by_espn_id: dict[str, Situation | None] = {}
    by_teams: dict[tuple[str, str], tuple[str, Situation | None]] = {}
    for event in scoreboard.events:
        competition = event.competitions[0]
        by_espn_id[event.id] = competition.situation

        by_side = {c.home_away: c.team.abbreviation for c in competition.competitors}
        home = ESPN_ABBREV_CORRECTIONS.get(by_side.get("home", ""), by_side.get("home"))
        away = ESPN_ABBREV_CORRECTIONS.get(by_side.get("away", ""), by_side.get("away"))
        if home and away:
            by_teams[(home, away)] = (event.id, competition.situation)
    return by_espn_id, by_teams


def load_game_snapshots() -> None:
    """capture a snapshot for every currently-live game"""
    client = D1Client(**get_d1_config())

    live_games = client.query(
        "SELECT g.game_id, g.cbs_event_id, g.espn_event_id, "
        "s.latitude, s.longitude, s.roof_type, "
        "ht.abbreviation AS home_abbrev, at.abbreviation AS away_abbrev "
        "FROM games g "
        "LEFT JOIN stadiums s ON s.stadium_id = g.stadium_id "
        "JOIN teams ht ON ht.team_id = g.home_team_id "
        "JOIN teams at ON at.team_id = g.away_team_id "
        "WHERE g.status IN ('IN_PROGRESS', 'HALFTIME')"
    ).results
    if not live_games:
        logger.info("No live games to snapshot")
        return

    data = get_cbs_pool_home()
    if data is None:
        return
    events_by_cbs_id = {e.cbs_event_id: e for e in data.pool_period.pool_events}

    espn_by_id, espn_by_teams = _fetch_espn_scoreboard_lookup()
    espn_backfill_statements: list[tuple[str, list[Any] | None]] = []

    game_ids = [row["game_id"] for row in live_games]
    latest_by_game_id = {
        row["game_id"]: row
        for row in client.query(
            _LATEST_SNAPSHOT_SQL.format(", ".join("?" * len(game_ids))), game_ids
        ).results
    }

    statements: list[tuple[str, list[Any] | None]] = []
    gap_statements: list[tuple[str, list[Any] | None]] = []
    skipped_unchanged = 0
    for row in live_games:
        event = events_by_cbs_id.get(row["cbs_event_id"])
        if event is None:
            logger.warning(
                "No CBS event for live game_id=%s (cbs_event_id=%s) - skipping snapshot",
                row["game_id"],
                row["cbs_event_id"],
            )
            continue

        if row["espn_event_id"] is not None:
            situation = espn_by_id.get(row["espn_event_id"])
            if espn_by_id and row["espn_event_id"] not in espn_by_id:
                logger.warning(
                    "Linked ESPN event %s for game_id=%s not found in current "
                    "scoreboard - leaving situation unset",
                    row["espn_event_id"],
                    row["game_id"],
                )
        else:
            team_key = (row["home_abbrev"], row["away_abbrev"])
            match = espn_by_teams.get(team_key)
            if match is None:
                situation = None
                if espn_by_teams:
                    logger.warning(
                        "No ESPN event for live game_id=%s (%s @ %s) - leaving situation unset",
                        row["game_id"],
                        row["away_abbrev"],
                        row["home_abbrev"],
                    )
                    gap_statements.append(
                        mapping_gap_statement(
                            "espn",
                            "team_pair",
                            f"{row['away_abbrev']}@{row['home_abbrev']}",
                            "load_game_snapshots",
                        )
                    )
            else:
                espn_event_id, situation = match
                espn_backfill_statements.append(
                    (_UPDATE_GAME_ESPN_ID_SQL, [espn_event_id, row["game_id"]])
                )

        # nothing happened since the prior snapshot - CBS's clock lags ESPN,
        # so a new ESPN play alone still counts as a change
        situation_fields = _situation_fields(situation)
        previous = latest_by_game_id.get(row["game_id"])
        if previous is not None and (
            previous["quarter"],
            previous["time_remaining"],
            previous["home_score"],
            previous["away_score"],
            previous["last_play_id"],
        ) == (
            event.game_period,
            event.time_remaining,
            event.home_team_score,
            event.away_team_score,
            situation_fields[-1],
        ):
            skipped_unchanged += 1
            continue

        statements.append(
            (
                _INSERT_SNAPSHOT_SQL,
                [
                    row["game_id"],
                    event.game_period,
                    event.time_remaining,
                    event.game_status_desc,
                    event.possession if event.possession != "NONE" else None,
                    event.home_team_score,
                    event.away_team_score,
                    *situation_fields,
                    *_snapshot_weather(row, previous),
                ],
            )
        )

    if espn_backfill_statements:
        sql_batch_call(espn_backfill_statements, client)

    if skipped_unchanged:
        logger.info(
            "Skipped %d snapshot(s) - nothing changed since last capture",
            skipped_unchanged,
        )

    if not statements:
        if not skipped_unchanged:
            logger.warning("No snapshots captured")
        if gap_statements:
            sql_batch_call(gap_statements, client)
        return

    sql_batch_call(statements + gap_statements, client)
    logger.info("Captured %d game snapshots", len(statements))


def main() -> None:
    """game snapshots"""
    load_game_snapshots()


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    main()
