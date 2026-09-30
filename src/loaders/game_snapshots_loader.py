"""
Capture a snapshot (score/quarter/clock/possession/field position + weather)
for every game in progress, from ESPN's public scoreboard (one call covers
every game) and Pirate Weather for conditions at the stadium's location.

ESPN is the fastest of the three live sources - confirmed live 2026-09-27
that Sports IO's clock sat still for 2+ minutes while ESPN's ran, and
CBS's clock lags ESPN too - so this is what src/live_ticker.py polls every
15 seconds during games.

Usage: uv run python -m src.loaders.game_snapshots_loader [local|prod]
"""

import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from api.espn_client import get_scoreboard, team_pair
from api.espn_models import Competition, Situation
from config.config import configure_logging, load_env
from db.clients import get_d1
from db.d1_client import D1Client, Statement
from src.game_rules import DONE_STATUSES, LIVE_WINDOW_HOURS, sql_list
from src.loaders.loader_helper import (
    capture_weather,
    mapping_gap_statement,
    sql_batch_call,
)
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

_INSERT_SNAPSHOT_SQL = """
INSERT INTO game_snapshots (
    game_id, quarter, time_remaining, status_desc, possession, home_score, away_score,
    down, distance, yard_line, down_distance_text, possession_text,
    is_red_zone, home_timeouts, away_timeouts,
    last_play_text, last_play_type, drive_text, drive_start_yard_line,
    drive_start_text, home_win_pct, away_win_pct, last_play_id,
    temperature_f, feels_like_f, weather_condition, weather_icon, precip_type,
    wind_speed_mph, wind_gust_mph, wind_direction, precipitation_pct,
    visibility_mi, weather_alerts_json, weather_captured_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

# game_snapshots' weather columns, in Weather's field order
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


class GameState(NamedTuple):
    """clock and score from ESPN's competition - quarter 5 is overtime, same
    as ESPN's period; possession is None when ESPN has no one on offense
    (between quarters, halftime). game_snapshots column order."""

    quarter: int | None
    time_remaining: str | None
    status_desc: str | None
    possession: str | None
    home_score: int | None
    away_score: int | None


class PlaySituation(NamedTuple):
    """ESPN's down/distance/last play/win probability - all None if ESPN has
    no situation for this game right now (e.g. halftime). game_snapshots
    column order."""

    down: int | None = None
    distance: int | None = None
    yard_line: int | None = None
    down_distance_text: str | None = None
    possession_text: str | None = None
    is_red_zone: bool | None = None
    home_timeouts: int | None = None
    away_timeouts: int | None = None
    last_play_text: str | None = None
    last_play_type: str | None = None
    drive_text: str | None = None
    drive_start_yard_line: int | None = None
    drive_start_text: str | None = None
    home_win_pct: float | None = None
    away_win_pct: float | None = None
    last_play_id: str | None = None


_NO_SITUATION = PlaySituation()


def _win_pct(fraction: float | None) -> float | None:
    return None if fraction is None else round(fraction * 100, 1)


def _situation_fields(situation: Situation | None) -> PlaySituation:
    """None across the board if ESPN has no situation for this game right
    now - e.g. between plays like halftime, or if ESPN's
    abbreviation-matched event wasn't found."""
    if situation is None:
        return _NO_SITUATION

    last_play = situation.last_play
    probability = last_play.probability if last_play else None
    drive = last_play.drive if last_play else None
    drive_start = drive.start if drive else None
    return PlaySituation(
        down=situation.down,
        distance=situation.distance,
        yard_line=situation.yard_line,
        down_distance_text=situation.down_distance_text,
        possession_text=situation.possession_text,
        is_red_zone=situation.is_red_zone,
        home_timeouts=situation.home_timeouts,
        away_timeouts=situation.away_timeouts,
        # ESPN sometimes pads this with a leading space
        last_play_text=last_play.text.strip() if last_play and last_play.text else None,
        last_play_type=last_play.type.text if last_play and last_play.type else None,
        drive_text=drive.description if drive else None,
        drive_start_yard_line=drive_start.yard_line if drive_start else None,
        drive_start_text=drive_start.text if drive_start else None,
        home_win_pct=_win_pct(probability.home_win_percentage) if probability else None,
        away_win_pct=_win_pct(probability.away_win_percentage) if probability else None,
        last_play_id=last_play.id if last_play else None,
    )


def _snapshot_weather(
    row: dict[str, Any], previous: dict[str, Any] | None
) -> tuple[Any, ...]:
    """capture_weather()'s Weather plus weather_captured_at. Reuses the
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

# games that could be live right now - ESPN's own status decides which of
# these actually are (kickoff shows up there before our Sports IO-sourced
# games.status catches up)
_CANDIDATE_GAMES_SQL = f"""
SELECT g.game_id, g.espn_event_id,
    s.latitude, s.longitude, s.roof_type,
    ht.abbreviation AS home_abbrev, at.abbreviation AS away_abbrev
FROM games g
LEFT JOIN stadiums s ON s.stadium_id = g.stadium_id
JOIN teams ht ON ht.team_id = g.home_team_id
JOIN teams at ON at.team_id = g.away_team_id
WHERE g.game_time <= ? AND g.game_time >= ?
  AND (g.status IS NULL OR g.status NOT IN {sql_list(DONE_STATUSES)})
"""


def _fetch_espn_lookup() -> tuple[
    dict[str, Competition], dict[tuple[str, str], tuple[str, Competition]]
]:
    """ESPN competitions by event id, and by (home, away) abbreviation for
    games whose espn_event_id isn't linked yet. Empty on a failed fetch -
    the caller skips this round rather than raising."""
    try:
        scoreboard = get_scoreboard()
    except Exception:
        logger.exception("ESPN scoreboard fetch failed - no snapshots this round")
        return {}, {}

    by_espn_id: dict[str, Competition] = {}
    by_teams: dict[tuple[str, str], tuple[str, Competition]] = {}
    for event in scoreboard.events:
        competition = event.competitions[0]
        by_espn_id[event.id] = competition

        home, away = team_pair(competition)
        if home and away:
            by_teams[(home, away)] = (event.id, competition)
    return by_espn_id, by_teams


def _game_state_fields(competition: Competition) -> GameState:
    by_side = {c.home_away: c for c in competition.competitors}
    home, away = by_side.get("home"), by_side.get("away")
    situation = competition.situation
    possession = None
    if situation is not None and situation.possession_team_id is not None:
        if home is not None and situation.possession_team_id == home.team.id:
            possession = "HOME"
        elif away is not None and situation.possession_team_id == away.team.id:
            possession = "AWAY"
    return GameState(
        quarter=competition.status.period,
        time_remaining=competition.status.display_clock,
        status_desc=competition.status.type.name,
        possession=possession,
        home_score=int(home.score)
        if home is not None and home.score.isdigit()
        else None,
        away_score=int(away.score)
        if away is not None and away.score.isdigit()
        else None,
    )


def _candidate_games(client: D1Client) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return client.query(
        _CANDIDATE_GAMES_SQL,
        [
            utc_iso(now),
            utc_iso(now - timedelta(hours=LIVE_WINDOW_HOURS)),
        ],
    ).results


def has_candidate_games() -> bool:
    """whether any game could be live right now - one D1 query, no ESPN
    call, so src/live_ticker.py can exit straight away outside game time"""
    return bool(_candidate_games(get_d1()))


def load_game_snapshots() -> set[int]:
    """capture a snapshot for every game ESPN reports as in progress -
    returns the game_ids that got a new row (nothing changed means no row),
    so the caller only rewrites KV when something actually moved"""
    client = get_d1()

    candidates = _candidate_games(client)
    if not candidates:
        logger.info("No live games to snapshot")
        return set()

    espn_by_id, espn_by_teams = _fetch_espn_lookup()
    if not espn_by_id:
        return set()

    gap_statements: list[Statement] = []
    espn_backfill_statements: list[Statement] = []
    live: list[tuple[dict[str, Any], Competition]] = []
    for row in candidates:
        if row["espn_event_id"] is not None:
            competition = espn_by_id.get(row["espn_event_id"])
        else:
            match = espn_by_teams.get((row["home_abbrev"], row["away_abbrev"]))
            competition = None
            if match is None:
                logger.warning(
                    "No ESPN event for game_id=%s (%s @ %s)",
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
                espn_event_id, competition = match
                espn_backfill_statements.append(
                    (_UPDATE_GAME_ESPN_ID_SQL, [espn_event_id, row["game_id"]])
                )
        # 'in' covers halftime and delays too; 'pre'/'post' aren't live
        if competition is not None and competition.status.type.state == "in":
            live.append((row, competition))

    if espn_backfill_statements or gap_statements:
        sql_batch_call(espn_backfill_statements + gap_statements, client)
    if not live:
        logger.info("No games in progress on ESPN")
        return set()

    game_ids = [row["game_id"] for row, _ in live]
    latest_by_game_id = {
        row["game_id"]: row
        for row in client.query(
            _LATEST_SNAPSHOT_SQL.format(", ".join("?" * len(game_ids))), game_ids
        ).results
    }

    statements: list[Statement] = []
    captured: set[int] = set()
    for row, competition in live:
        state = _game_state_fields(competition)
        situation = _situation_fields(competition.situation)
        previous = latest_by_game_id.get(row["game_id"])
        # nothing happened since the prior snapshot - clock, score and
        # ESPN's latest play id all unchanged
        if previous is not None and (
            previous["quarter"],
            previous["time_remaining"],
            previous["home_score"],
            previous["away_score"],
            previous["last_play_id"],
        ) == (
            state.quarter,
            state.time_remaining,
            state.home_score,
            state.away_score,
            situation.last_play_id,
        ):
            continue

        statements.append(
            (
                _INSERT_SNAPSHOT_SQL,
                [
                    row["game_id"],
                    *state,
                    *situation,
                    *_snapshot_weather(row, previous),
                ],
            )
        )
        captured.add(row["game_id"])

    if statements:
        sql_batch_call(statements, client)
    logger.info(
        "Captured %d game snapshot(s), %d unchanged",
        len(statements),
        len(live) - len(statements),
    )
    return captured


def main() -> None:
    """game snapshots"""
    load_game_snapshots()


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    main()
