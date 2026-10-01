"""meta:admin - see src/CLAUDE.md's KV writer section."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from db.clients import get_d1, get_kv
from src.timestamps import parse_utc_iso, utc_iso

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
SELECT COUNT(*) AS distinct_count, COALESCE(SUM(occurrences), 0) AS total_occurrences,
    COALESCE(SUM(last_seen_at >= ?), 0) AS active_count
FROM system_events
"""

_SYSTEM_EVENTS_RECENT_SQL = """
SELECT source, message, first_seen_at, last_seen_at, occurrences
FROM system_events
ORDER BY last_seen_at DESC
LIMIT 20
"""

# Deliberately more generous than orchestration.py's own intervals - a
# long live-heavy Sunday can legitimately delay the quiet-only tasks
# (odds baseline, housekeeping, CBS quiet picks poll) for hours without
# anything actually being wrong. Not imported from orchestration.py
# directly to avoid a circular import (orchestration.py already imports
# from this package); duplicated here as a deliberately looser,
# presentation-layer judgment call rather than the exact operational
# cadence. Roughly twice each task's real interval, or its longest
# plausible live-window delay for the quiet-only ones.
_ODDS_STALE_SECONDS = 12 * 60 * 60  # 6h baseline, quiet only
_HOUSEKEEPING_STALE_SECONDS = 48 * 60 * 60  # 24h, quiet only
_CBS_PICKS_QUIET_STALE_SECONDS = 12 * 60 * 60  # 30min, quiet only
_PREGAME_WEATHER_STALE_SECONDS = 8 * 60 * 60  # 4h baseline, every tick
_USER_PROFILES_STALE_SECONDS = 2 * 60 * 60  # 30min, every tick
_RECAP_STALE_SECONDS = 30 * 60  # 5min, every tick

# a system_events row counts as active (still happening) if it recurred
# this recently - rows never age out, so without this a month-old failure
# looks the same as one from a minute ago
_SYSTEM_EVENT_ACTIVE_SECONDS = 24 * 60 * 60


def _seconds_since(iso_value: str | None, now: datetime) -> float | None:
    """None if the key has never run, or isn't a plain ISO8601 UTC
    timestamp (deadline_last_synced_sunday stores a bare date, not one)."""
    if iso_value is None:
        return None
    try:
        last_at = parse_utc_iso(iso_value)
    except ValueError:
        return None
    return (now - last_at).total_seconds()


def _is_stale(ages: list[float | None], limit_seconds: int) -> bool:
    """Stale if no cursor has ever succeeded, or the most recent success
    across them is older than limit_seconds."""
    known = [age for age in ages if age is not None]
    return not known or min(known) > limit_seconds


def write_admin_status() -> None:
    """Write meta:admin - a health-check summary for an admin page: when
    each orchestration task last ran, and open mapping_gaps/system_events
    to review. Recomputed unconditionally every tick (see orchestration.py's
    main()) since it's a handful of cheap local SELECTs and freshness
    matters most exactly when something just broke.

    See the comment above last_run below for what last_at vs
    last_success_at mean and which tasks get a stale flag.
    """
    d1 = get_d1()
    now = datetime.now(UTC)

    state = {
        row["key"]: row["value"] for row in d1.query(_ORCHESTRATION_STATE_SQL).results
    }

    def age(key: str) -> float | None:
        return _seconds_since(state.get(key), now)

    # last_at is the scheduling cursor, bumped on every attempt - every
    # task in orchestration.py runs through scheduling.soft(), so the
    # cursor moves even when the attempt failed. last_success_at is a
    # separate cursor set only when the task worked. Staleness always
    # compares against the success cursor, so a task that fails every time
    # doesn't look healthy just because its cursor keeps moving.
    #
    # Staleness is only flagged for tasks expected to run regardless of
    # live/quiet state. The six live-only pollers just report timestamps:
    # "should this have run" for those depends on live-window history, and
    # most of the week they correctly haven't run because nothing's live.
    last_run: dict[str, Any] = {
        # Odds has two independent cursors (the flat baseline and the
        # pre-kickoff capture, see orchestration.py) - either one
        # succeeding recently means odds data is fresh.
        "odds": {
            "baseline_last_at": state.get("odds_last_call_at"),
            "baseline_last_success_at": state.get("odds_last_success_at"),
            "prekickoff_last_at": state.get("odds_prekickoff_last_call_at"),
            "prekickoff_last_success_at": state.get("odds_prekickoff_last_success_at"),
            "stale": _is_stale(
                [age("odds_last_success_at"), age("odds_prekickoff_last_success_at")],
                _ODDS_STALE_SECONDS,
            ),
        },
        "housekeeping": {
            "last_at": state.get("housekeeping_last_run_at"),
            "last_success_at": state.get("housekeeping_last_success_at"),
            "stale": _is_stale(
                [age("housekeeping_last_success_at")], _HOUSEKEEPING_STALE_SECONDS
            ),
        },
        "cbs_picks_quiet_poll": {
            "last_at": state.get("cbs_picks_quiet_last_poll_at"),
            "last_success_at": state.get("cbs_picks_quiet_last_success_at"),
            "stale": _is_stale(
                [age("cbs_picks_quiet_last_success_at")],
                _CBS_PICKS_QUIET_STALE_SECONDS,
            ),
        },
        "pregame_weather_capture": {
            "last_at": state.get("weather_pregame_last_capture_at"),
            "last_success_at": state.get("weather_pregame_last_success_at"),
            "stale": _is_stale(
                [age("weather_pregame_last_success_at")],
                _PREGAME_WEATHER_STALE_SECONDS,
            ),
        },
        "user_profiles_write": {
            "last_at": state.get("user_profiles_last_write_at"),
            "last_success_at": state.get("user_profiles_last_success_at"),
            "stale": _is_stale(
                [age("user_profiles_last_success_at")], _USER_PROFILES_STALE_SECONDS
            ),
        },
        "recap_write": {
            "last_at": state.get("recap_last_write_at"),
            "last_success_at": state.get("recap_last_success_at"),
            "stale": _is_stale([age("recap_last_success_at")], _RECAP_STALE_SECONDS),
        },
        "sports_io_live_poll": {
            "last_at": state.get("sports_io_live_last_poll_at"),
            "last_success_at": state.get("sports_io_live_last_success_at"),
        },
        "cbs_live_poll": {
            "last_at": state.get("cbs_live_last_poll_at"),
            "last_success_at": state.get("cbs_live_last_success_at"),
        },
        "game_snapshot_capture": {
            "last_at": state.get("game_snapshot_last_capture_at"),
            "last_success_at": state.get("game_snapshot_last_success_at"),
        },
        "live_game_stats_capture": {
            "last_at": state.get("live_game_stats_last_capture_at"),
            "last_success_at": state.get("live_game_stats_last_success_at"),
        },
        "live_player_stats_capture": {
            "last_at": state.get("live_player_stats_last_capture_at"),
            "last_success_at": state.get("live_player_stats_last_success_at"),
        },
        "win_probability_capture": {
            "last_at": state.get("win_probability_last_run_at"),
            "last_success_at": state.get("win_probability_last_success_at"),
        },
        "standings_refresh": {
            "last_at": state.get("standings_last_run_at"),
            "last_success_at": state.get("standings_last_success_at"),
        },
        "team_profiles_write": {
            "last_at": state.get("team_profiles_last_run_at"),
            "last_success_at": state.get("team_profiles_last_success_at"),
        },
        "scoring_plays_refresh": {
            "last_at": state.get("scoring_plays_last_run_at"),
            "last_success_at": state.get("scoring_plays_last_success_at"),
        },
        "deadline_last_synced_sunday": state.get("deadline_last_synced_sunday"),
    }

    mapping_gaps_totals = d1.query(_MAPPING_GAPS_TOTALS_SQL).results[0]
    active_since = utc_iso(now - timedelta(seconds=_SYSTEM_EVENT_ACTIVE_SECONDS))
    system_events_totals = d1.query(_SYSTEM_EVENTS_TOTALS_SQL, [active_since]).results[
        0
    ]
    recent_events = [
        {**row, "active": row["last_seen_at"] >= active_since}
        for row in d1.query(_SYSTEM_EVENTS_RECENT_SQL).results
    ]

    get_kv().write(
        "meta:admin",
        {
            "updated_at": utc_iso(now),
            "last_run": last_run,
            "mapping_gaps": {
                "distinct_count": mapping_gaps_totals["distinct_count"],
                "total_occurrences": mapping_gaps_totals["total_occurrences"],
                "recent": d1.query(_MAPPING_GAPS_RECENT_SQL).results,
            },
            "system_events": {
                "distinct_count": system_events_totals["distinct_count"],
                "total_occurrences": system_events_totals["total_occurrences"],
                # recurred in the last _SYSTEM_EVENT_ACTIVE_SECONDS
                "active_count": system_events_totals["active_count"],
                "recent": recent_events,
            },
        },
    )
    logger.info(
        "Wrote meta:admin (%d mapping gaps, %d system events) to KV",
        mapping_gaps_totals["distinct_count"],
        system_events_totals["distinct_count"],
    )
