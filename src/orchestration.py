"""Recurring, trigger-based scheduling - one stateless tick per invocation.

Meant to be invoked by cron every minute (`* * * * *`); most ticks do almost
nothing (a couple of cheap local D1 checks)

Usage: uv run python -m src.orchestration [local|prod]
"""

import logging
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from config.config import configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.loaders.cbs_loader import load_cbs_games, load_cbs_user_picks, load_cbs_weeks
from src.loaders.game_snapshots_loader import load_game_snapshots
from src.loaders.odds_loader import load_the_odds_api_odds
from src.loaders.sports_io_loader import (
    load_game_statistics,
    load_games_data,
    load_live_game_statistics,
)

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

# A game is "live" from its scheduled kickoff until this many hours later,
# regardless of what our last-known status says (status might just be
# stale - that's exactly what the live poll is for).
LIVE_WINDOW_HOURS = 4

SPORTS_IO_LIVE_INTERVAL_SECONDS = 60
CBS_LIVE_INTERVAL_SECONDS = 120
GAME_SNAPSHOT_INTERVAL_SECONDS = (
    3 * 60
)  # score/quarter/weather don't need finer granularity
ODDS_INTERVAL_SECONDS = (
    6 * 60 * 60
)  # 4x/day baseline - no game-day boost yet, see CLAUDE.md
HOUSEKEEPING_INTERVAL_SECONDS = 24 * 60 * 60

_UPSERT_STATE_SQL = """
INSERT INTO orchestration_state (key, value) VALUES (?, ?)
ON CONFLICT(key) DO UPDATE SET value = excluded.value
"""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_state(client: D1Client, key: str) -> str | None:
    result = client.query("SELECT value FROM orchestration_state WHERE key = ?", [key])
    return result.results[0]["value"] if result.results else None


def _set_state(client: D1Client, key: str, value: str) -> None:
    client.batch([(_UPSERT_STATE_SQL, [key, value])])


def _should_run(client: D1Client, key: str, min_interval_seconds: int) -> bool:
    last = _get_state(client, key)
    if last is None:
        return True
    last_dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (datetime.now(UTC) - last_dt).total_seconds() >= min_interval_seconds


def _current_week_deadline_utc(now_utc: datetime) -> datetime:
    """current week's sunday 1pm eastern (deadline for pickem)"""
    now_et = now_utc.astimezone(EASTERN)
    tue_indexed_weekday = (now_et.weekday() - 1) % 7  # Tue=0 .. Mon=6
    days_to_sunday = 5 - tue_indexed_weekday  # Sunday is day 5 of a Tue-start week
    target_sunday = (now_et + timedelta(days=days_to_sunday)).date()
    deadline_et = datetime(
        target_sunday.year,
        target_sunday.month,
        target_sunday.day,
        13,
        0,
        tzinfo=EASTERN,
    )
    return deadline_et.astimezone(UTC)


def _is_live_window_active(client: D1Client) -> bool:
    now = _now_iso()
    cutoff = (datetime.now(UTC) - timedelta(hours=LIVE_WINDOW_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    result = client.query(
        "SELECT 1 FROM games WHERE game_time <= ? AND game_time >= ? "
        "AND status NOT IN ('FINAL', 'CANCELLED', 'POSTPONED') LIMIT 1",
        [now, cutoff],
    )
    return bool(result.results)


def _run_live_updates(client: D1Client, env: str, now: datetime) -> None:
    if _should_run(
        client, "sports_io_live_last_poll_at", SPORTS_IO_LIVE_INTERVAL_SECONDS
    ):
        load_games_data(env, live=True)
        _set_state(client, "sports_io_live_last_poll_at", _now_iso())

    # need to poll for picks after the deadline or early week games
    # live polling only happens prior to deadline for seeding the data
    # data from sports io/espn fills in the majority of the live game data
    if now < _current_week_deadline_utc(now) and _should_run(
        client, "cbs_live_last_poll_at", CBS_LIVE_INTERVAL_SECONDS
    ):
        load_cbs_user_picks(env)
        _set_state(client, "cbs_live_last_poll_at", _now_iso())

    if _should_run(
        client, "game_snapshot_last_capture_at", GAME_SNAPSHOT_INTERVAL_SECONDS
    ):
        load_game_snapshots(env)
        _set_state(client, "game_snapshot_last_capture_at", _now_iso())

    if _should_run(
        client, "live_game_stats_last_capture_at", GAME_SNAPSHOT_INTERVAL_SECONDS
    ):
        load_live_game_statistics(env)
        _set_state(client, "live_game_stats_last_capture_at", _now_iso())


def _run_quiet_period_tasks(client: D1Client, env: str) -> None:
    if _should_run(client, "odds_last_call_at", ODDS_INTERVAL_SECONDS):
        load_the_odds_api_odds(env)
        _set_state(client, "odds_last_call_at", _now_iso())

    if _should_run(client, "housekeeping_last_run_at", HOUSEKEEPING_INTERVAL_SECONDS):
        load_games_data(env)  # full schedule/weeks refresh - idempotent, safe any day
        load_cbs_weeks(env)
        load_cbs_games(env)
        _set_state(client, "housekeeping_last_run_at", _now_iso())
    else:
        logger.info("Not running, too soon")


def _run_deadline_sweep(client: D1Client, env: str, now: datetime) -> None:
    deadline = _current_week_deadline_utc(now)
    sunday_date = deadline.date().isoformat()
    if now < deadline:
        return
    if _get_state(client, "deadline_last_synced_sunday") == sunday_date:
        return

    load_cbs_weeks(env)
    load_cbs_games(env)
    load_cbs_user_picks(env)
    _set_state(client, "deadline_last_synced_sunday", sunday_date)
    logger.info("Ran Sunday 1PM ET deadline sweep for %s", sunday_date)


def _run_finished_game_stats(client: D1Client, env: str) -> None:
    """Games that finished but have no game_team_stats rows yet - catches
    both the normal live->FINAL transition and anything missed if the
    process wasn't running at the time."""
    result = client.query(
        "SELECT DISTINCT w.week_number FROM games g "
        "JOIN weeks w ON w.week_id = g.week_id "
        "WHERE g.status = 'FINAL' "
        "AND NOT EXISTS (SELECT 1 FROM game_team_stats s WHERE s.game_id = g.game_id)"
    )
    for row in result.results:
        load_game_statistics(row["week_number"], env)


def run_tick(env: str = "local") -> None:
    if not load_env(env):
        sys.exit(1)

    client = D1Client(**get_d1_config())
    now = datetime.now(UTC)

    if _is_live_window_active(client):
        _run_live_updates(client, env, now)
    else:
        _run_quiet_period_tasks(client, env)

    _run_deadline_sweep(client, env, now)
    _run_finished_game_stats(client, env)


def main(env: str = "local") -> None:
    run_tick(env)


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
