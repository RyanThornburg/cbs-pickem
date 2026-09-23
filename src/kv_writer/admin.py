"""meta:admin - see src/CLAUDE.md's KV writer section."""

import logging
from datetime import UTC, datetime
from typing import Any

from config.config import get_d1_config, get_kv_config
from db.d1_client import D1Client
from db.kv_client import KVClient

logger = logging.getLogger(__name__)

# src/orchestration.py's polling cursors - lets this health check answer
# "when did each thing last actually run" without duplicating that logic.
_ORCHESTRATION_STATE_SQL = "SELECT key, value FROM orchestration_state"

_MAPPING_GAPS_TOTALS_SQL = """
SELECT COUNT(*) AS distinct_count, COALESCE(SUM(occurrences), 0) AS total_occurrences
FROM mapping_gaps
"""

_MAPPING_GAPS_RECENT_SQL = """
SELECT source, entity_type, raw_value, context, first_seen_at, last_seen_at, occurrences
FROM mapping_gaps
ORDER BY last_seen_at DESC
LIMIT 20
"""

_SYSTEM_EVENTS_TOTALS_SQL = """
SELECT COUNT(*) AS distinct_count, COALESCE(SUM(occurrences), 0) AS total_occurrences
FROM system_events
"""

_SYSTEM_EVENTS_RECENT_SQL = """
SELECT source, message, first_seen_at, last_seen_at, occurrences
FROM system_events
ORDER BY last_seen_at DESC
LIMIT 20
"""

# Deliberately more generous than orchestration.py's own
# ODDS_INTERVAL_SECONDS (6h)/HOUSEKEEPING_INTERVAL_SECONDS (24h) - a long
# live-heavy Sunday can legitimately delay the quiet-only branch these
# gate for hours without anything actually being wrong. Not imported from
# orchestration.py directly to avoid a circular import (orchestration.py
# already imports from this package); duplicated here as a deliberately
# looser, presentation-layer judgment call rather than the exact operational
# cadence.
_ODDS_STALE_SECONDS = 12 * 60 * 60
_HOUSEKEEPING_STALE_SECONDS = 48 * 60 * 60


def _seconds_since(iso_value: str | None, now: datetime) -> float | None:
    """None if the key has never run, or isn't a plain ISO8601 UTC
    timestamp (deadline_last_synced_sunday stores a bare date, not one)."""
    if iso_value is None:
        return None
    try:
        last_at = datetime.strptime(iso_value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (now - last_at).total_seconds()


def write_admin_status() -> None:
    """Write meta:admin - a health-check summary for an admin page: when
    each orchestration task last ran, and open mapping_gaps/system_events
    to review. Recomputed unconditionally every tick (see orchestration.py's
    run_tick()) since it's a handful of cheap local SELECTs and freshness
    matters most exactly when something just broke.

    Staleness is only flagged for odds/housekeeping - the tasks expected to
    run eventually regardless of live/quiet state. The four live-only
    pollers (sports_io/cbs live polls, game_snapshot, live_game_stats) just
    report their raw last-run timestamp with no stale flag: "should this
    have run" for those depends on live-window history, which isn't worth
    the complexity for a first pass - most weeks they simply won't have run
    recently because nothing's live, and that's correct, not a problem.
    """
    d1 = D1Client(**get_d1_config())
    now = datetime.now(UTC)

    state = {
        row["key"]: row["value"] for row in d1.query(_ORCHESTRATION_STATE_SQL).results
    }

    # Odds has two independent cursors (the flat baseline and the
    # pre-kickoff capture, see orchestration.py) - either one running
    # recently means odds data is fresh, so staleness compares against
    # whichever last ran more recently.
    odds_ages = [
        age
        for age in (
            _seconds_since(state.get("odds_last_call_at"), now),
            _seconds_since(state.get("odds_prekickoff_last_call_at"), now),
        )
        if age is not None
    ]
    odds_age = min(odds_ages) if odds_ages else None
    housekeeping_age = _seconds_since(state.get("housekeeping_last_run_at"), now)

    last_run: dict[str, Any] = {
        "odds": {
            "baseline_last_at": state.get("odds_last_call_at"),
            "prekickoff_last_at": state.get("odds_prekickoff_last_call_at"),
            "stale": odds_age is None or odds_age > _ODDS_STALE_SECONDS,
        },
        "housekeeping": {
            "last_at": state.get("housekeeping_last_run_at"),
            "stale": housekeeping_age is None
            or housekeeping_age > _HOUSEKEEPING_STALE_SECONDS,
        },
        "sports_io_live_poll": {"last_at": state.get("sports_io_live_last_poll_at")},
        "cbs_live_poll": {"last_at": state.get("cbs_live_last_poll_at")},
        "game_snapshot_capture": {
            "last_at": state.get("game_snapshot_last_capture_at")
        },
        "live_game_stats_capture": {
            "last_at": state.get("live_game_stats_last_capture_at")
        },
        "deadline_last_synced_sunday": state.get("deadline_last_synced_sunday"),
    }

    mapping_gaps_totals = d1.query(_MAPPING_GAPS_TOTALS_SQL).results[0]
    system_events_totals = d1.query(_SYSTEM_EVENTS_TOTALS_SQL).results[0]

    kv = KVClient(**get_kv_config())
    kv.write(
        "meta:admin",
        {
            "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_run": last_run,
            "mapping_gaps": {
                "distinct_count": mapping_gaps_totals["distinct_count"],
                "total_occurrences": mapping_gaps_totals["total_occurrences"],
                "recent": d1.query(_MAPPING_GAPS_RECENT_SQL).results,
            },
            "system_events": {
                "distinct_count": system_events_totals["distinct_count"],
                "total_occurrences": system_events_totals["total_occurrences"],
                "recent": d1.query(_SYSTEM_EVENTS_RECENT_SQL).results,
            },
        },
    )
    logger.info(
        "Wrote meta:admin (%d mapping gaps, %d system events) to KV",
        mapping_gaps_totals["distinct_count"],
        system_events_totals["distinct_count"],
    )
