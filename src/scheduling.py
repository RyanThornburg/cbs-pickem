"""Shared plumbing for the cron-driven processes - src/orchestration.py (the
once-a-minute tick) and src/live_ticker.py (the 15-second live loop): the
orchestration_state cursors, the soft-fail task wrapper that records
system_events, and the one-run-at-a-time process lock. See src/CLAUDE.md's
Orchestration section."""

import fcntl
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TextIO

from config.config import LOCK_DIR
from db.d1_client import D1Client

logger = logging.getLogger(__name__)

# cron fires every 60s but each cursor is stamped when its task *finishes*,
# so the next tick lands a few seconds short of the interval (the whole tick
# up to that point, not just this task) - without this
# slack a 60s task ran every 2 minutes and a 3-minute one every 4
# (confirmed live against game_snapshots spacing, 2026-09-27)
_SHOULD_RUN_SLACK_SECONDS = 30

_UPSERT_STATE_SQL = """
INSERT INTO orchestration_state (key, value) VALUES (?, ?)
ON CONFLICT(key) DO UPDATE SET value = excluded.value
"""

# first sighting of a (source, message) pair inserts a row later ones increase count and last seen
_UPSERT_SYSTEM_EVENT_SQL = """
INSERT INTO system_events (source, message, first_seen_at, last_seen_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(source, message) DO UPDATE SET
    last_seen_at = excluded.last_seen_at,
    occurrences = occurrences + 1
"""


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_state(client: D1Client, key: str) -> str | None:
    result = client.query("SELECT value FROM orchestration_state WHERE key = ?", [key])
    return result.results[0]["value"] if result.results else None


def set_state(client: D1Client, key: str, value: str) -> None:
    client.batch([(_UPSERT_STATE_SQL, [key, value])])


def should_run(client: D1Client, key: str, min_interval_seconds: int) -> bool:
    last = get_state(client, key)
    if last is None:
        return True
    last_dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - last_dt).total_seconds()
    return elapsed >= min_interval_seconds - _SHOULD_RUN_SLACK_SECONDS


def record_system_event(client: D1Client, source: str, message: str) -> None:
    now = now_iso()
    client.batch([(_UPSERT_SYSTEM_EVENT_SQL, [source, message[:500], now, now])])


def soft(client: D1Client, source: str, task: Callable[[], object]) -> bool:
    """Run one task, and on any exception log it (lands in error.log) and
    record a system_events row instead of raising - one failing source
    (a CBS scrape, a Sports IO payload that won't validate) must never end
    the run and take every later task with it. True if the task worked."""
    try:
        task()
        return True
    except Exception as exc:
        logger.exception("%s failed - continuing without it", source)
        record_system_event(client, source, str(exc))
        return False


def run_on_interval(
    client: D1Client,
    source: str,
    task: Callable[[], object],
    cursor_key: str,
    success_key: str,
    interval_seconds: int,
) -> None:
    """The standard interval task: run `task` via soft() once
    `interval_seconds` has passed since `cursor_key`. The cursor moves on
    every attempt, so a task that keeps failing retries once per interval
    rather than every tick; `success_key` only moves when it worked, which
    is what meta:admin reads to tell a failing task from a healthy one."""
    if not should_run(client, cursor_key, interval_seconds):
        return
    if soft(client, source, task):
        set_state(client, success_key, now_iso())
    set_state(client, cursor_key, now_iso())


def acquire_lock(name: str) -> TextIO | None:
    """Exclusive, non-blocking lock named `name` (e.g. "orchestration.prod"),
    or None if another process still holds it. Cron starts a run every
    minute whether or not the last one finished (a slow API with retries can
    run past 60s), and two overlapping runs would both pass the same
    should_run() checks and double every call - CBS logins included. The
    lock is released when the process exits, however it exits. Callers
    include the env in the name, so a local run never blocks a prod one.
    fcntl is Unix-only - see the README."""
    LOCK_DIR.mkdir(exist_ok=True)
    lock_file = open(LOCK_DIR / f"{name}.lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file
