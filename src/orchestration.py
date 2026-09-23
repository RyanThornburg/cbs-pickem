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
from src.kv_writer import (
    write_admin_status,
    write_current_week_games,
    write_current_week_leaderboard,
    write_current_week_odds,
    write_current_week_trends,
    write_incomplete_weeks_games,
    write_meta_current,
    write_season_trends,
    write_user_profiles,
)
from src.loaders.cbs_loader import load_cbs_games, load_cbs_user_picks, load_cbs_weeks
from src.loaders.game_snapshots_loader import load_game_snapshots
from src.loaders.odds_loader import load_the_odds_api_odds
from src.loaders.pregame_weather_loader import load_pregame_weather
from src.loaders.sports_io_loader import (
    load_game_statistics,
    load_games_data,
    load_live_game_statistics,
)
from src.loaders.teams_loader import main as load_teams

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
ODDS_INTERVAL_SECONDS = 6 * 60 * 60  # 4x/day baseline, all days
# Extra capture right before each distinct kickoff cluster (TNF, Sunday windows, MNF)
ODDS_PREKICKOFF_LEAD_MINUTES = 30
ODDS_PREKICKOFF_MIN_GAP_SECONDS = 2 * 60 * 60  # cover the 4pm window gap
HOUSEKEEPING_INTERVAL_SECONDS = 24 * 60 * 60
# quiet poll cbs data to see if user has entered picks for ui
CBS_PICKS_QUIET_INTERVAL_SECONDS = 30 * 60
# Pregame forecast: coarse baseline for the whole current week (scoped to
# weeks.is_current in load_pregame_weather() itself - see its own docstring
# for why that join matters), boosted once a game is close enough that a
# tighter refresh is actually worth the extra calls - well within Pirate
# Weather's 20k/month quota either way.
WEATHER_PREGAME_BASELINE_INTERVAL_SECONDS = 4 * 60 * 60
WEATHER_PREGAME_NEAR_INTERVAL_SECONDS = 60 * 60
WEATHER_PREGAME_NEAR_WINDOW_HOURS = 24
# Per-user profile KV keys (streaks/tendencies) - deliberately not on every
# tick like most other write_* calls below: one KV write per active user
# every single minute-cron tick would be a lot of avoidable write volume for
# data that only actually changes when picks get made/graded, not on every
# live score tick. Same cadence class as CBS_PICKS_QUIET_INTERVAL_SECONDS.
USER_PROFILES_INTERVAL_SECONDS = 30 * 60

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


def _record_system_event(client: D1Client, source: str, message: str) -> None:
    now = _now_iso()
    client.batch([(_UPSERT_SYSTEM_EVENT_SQL, [source, message[:500], now, now])])


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
        "AND (status IS NULL OR status NOT IN ('FINAL', 'CANCELLED', 'POSTPONED')) LIMIT 1",
        [now, cutoff],
    )
    return bool(result.results)


def _run_live_updates(client: D1Client) -> None:
    if _should_run(
        client, "sports_io_live_last_poll_at", SPORTS_IO_LIVE_INTERVAL_SECONDS
    ):
        # Sports IO has sent malformed/unexpected game data mid-slate
        try:
            load_games_data(live=True)
        except Exception as exc:
            logger.exception("Live games poll failed - continuing without it")
            _record_system_event(client, "sports_io_live_poll", str(exc))
        finally:
            _set_state(client, "sports_io_live_last_poll_at", _now_iso())

    # Runs for the whole live window, not just pre-deadline - most games kick
    # off at/after the Sunday 1PM ET deadline, and that's also when CBS
    # reveals every entry's picks (not just early-game ones), so this is when
    # the bulk of live pick-grading actually happens via trending status
    if _should_run(client, "cbs_live_last_poll_at", CBS_LIVE_INTERVAL_SECONDS):
        load_cbs_user_picks()
        # write_current_week_leaderboard() not needed here - run_tick()
        # now calls it unconditionally on every tick regardless of branch
        _set_state(client, "cbs_live_last_poll_at", _now_iso())

    if _should_run(
        client, "game_snapshot_last_capture_at", GAME_SNAPSHOT_INTERVAL_SECONDS
    ):
        load_game_snapshots()
        _set_state(client, "game_snapshot_last_capture_at", _now_iso())

    if _should_run(
        client, "live_game_stats_last_capture_at", GAME_SNAPSHOT_INTERVAL_SECONDS
    ):
        load_live_game_statistics()
        _set_state(client, "live_game_stats_last_capture_at", _now_iso())


def _capture_odds(client: D1Client, state_key: str) -> None:
    """Odds aren't required/shouldn't block, don't raise but log error"""
    try:
        load_the_odds_api_odds()
        write_current_week_odds()
    except Exception as exc:
        logger.exception("Odds capture failed - continuing without it")
        _record_system_event(client, "odds_capture", str(exc))
    finally:
        _set_state(client, state_key, _now_iso())


def _run_quiet_period_tasks(client: D1Client) -> None:
    if _should_run(client, "odds_last_call_at", ODDS_INTERVAL_SECONDS):
        _capture_odds(client, "odds_last_call_at")

    if _should_run(
        client, "cbs_picks_quiet_last_poll_at", CBS_PICKS_QUIET_INTERVAL_SECONDS
    ):
        load_cbs_user_picks()
        _set_state(client, "cbs_picks_quiet_last_poll_at", _now_iso())

    if _should_run(client, "housekeeping_last_run_at", HOUSEKEEPING_INTERVAL_SECONDS):
        load_games_data()  # full schedule/weeks refresh - idempotent, safe any day
        load_teams()  # refresh team win/loss/tie records, same cadence
        load_cbs_weeks()
        load_cbs_games()
        write_meta_current()
        # write_current_week_games() not needed here - run_tick() now
        # calls it unconditionally on every tick regardless of branch
        _set_state(client, "housekeeping_last_run_at", _now_iso())
    else:
        logger.info("Not running, too soon")


def _run_pre_kickoff_odds_capture(client: D1Client, now: datetime) -> None:
    """One extra odds call right before each distinct kickoff cluster this
    week (TNF, Sunday's early/late/night windows, MNF) - the flat baseline
    interval otherwise has no relationship to actual kickoff times and can
    miss the line right before games start. Keyed off games.game_time
    itself rather than hardcoded days/times, same reasoning as
    _is_live_window_active() - kickoff slots shift (byes, international
    games, Thanksgiving). ODDS_PREKICKOFF_MIN_GAP_SECONDS (not per-slot
    state) is what collapses a same-window doubleheader (e.g. Sunday's
    4:05/4:25 ET games) into a single call instead of one per exact
    game_time. Runs unconditionally every tick (not just quiet-period),
    since a still-live early game can otherwise suppress the call for an
    approaching later kickoff.
    """
    if not _should_run(
        client, "odds_prekickoff_last_call_at", ODDS_PREKICKOFF_MIN_GAP_SECONDS
    ):
        return

    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lead_cutoff = (now + timedelta(minutes=ODDS_PREKICKOFF_LEAD_MINUTES)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    result = client.query(
        "SELECT 1 FROM games WHERE game_time > ? AND game_time <= ? "
        "AND (status IS NULL OR status NOT IN ('FINAL', 'CANCELLED', 'POSTPONED')) LIMIT 1",
        [now_iso, lead_cutoff],
    )
    if not result.results:
        return

    logger.info("Running pre-kickoff odds capture")
    _capture_odds(client, "odds_prekickoff_last_call_at")


def _run_pregame_weather_capture(client: D1Client, now: datetime) -> None:
    """Pregame forecast for games that haven't started yet - same
    baseline+near-kickoff-boost shape as _run_pre_kickoff_odds_capture(),
    except continuous (weather is worth re-checking repeatedly as it
    changes) rather than a single pre-kickoff pulse. Runs unconditionally
    every tick, live or quiet, so an already-live early game can't
    suppress the refresh for an approaching later one.
    """
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    near_cutoff = (now + timedelta(hours=WEATHER_PREGAME_NEAR_WINDOW_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    has_near_kickoff = bool(
        client.query(
            "SELECT 1 FROM games WHERE game_time > ? AND game_time <= ? "
            "AND status = 'SCHEDULED' LIMIT 1",
            [now_iso, near_cutoff],
        ).results
    )
    interval = (
        WEATHER_PREGAME_NEAR_INTERVAL_SECONDS
        if has_near_kickoff
        else WEATHER_PREGAME_BASELINE_INTERVAL_SECONDS
    )
    if not _should_run(client, "weather_pregame_last_capture_at", interval):
        return

    try:
        load_pregame_weather()
    except Exception as exc:
        logger.exception("Pregame weather capture failed - continuing without it")
        _record_system_event(client, "pregame_weather_capture", str(exc))
    finally:
        _set_state(client, "weather_pregame_last_capture_at", _now_iso())


def _run_deadline_sweep(client: D1Client, now: datetime) -> None:
    deadline = _current_week_deadline_utc(now)
    sunday_date = deadline.date().isoformat()
    if now < deadline:
        return
    if _get_state(client, "deadline_last_synced_sunday") == sunday_date:
        return

    load_cbs_weeks()
    load_cbs_games()
    load_cbs_user_picks()
    write_meta_current()
    write_current_week_games()
    write_current_week_leaderboard()
    _set_state(client, "deadline_last_synced_sunday", sunday_date)
    logger.info("Ran Sunday 1PM ET deadline sweep for %s", sunday_date)


def _run_finished_game_stats(client: D1Client) -> None:
    """Games that went FINAL but haven't had their final box score
    reloaded yet - catches both the normal live->FINAL transition and
    anything missed if the process wasn't running at the time. Can't
    check "does game_team_stats have a row for this game" (an earlier
    version did, confirmed live 2026-09-10 to never fire) - live polling
    already writes game_team_stats rows well before a game goes FINAL, so
    a row always exists by the time this runs. `games.has_final_stats`
    tracks it explicitly instead.

    Also marks weeks.is_complete once every game in that week is FINAL -
    added 2026-09-15, this was a plain always-FALSE column with nothing
    anywhere ever writing to it until now. Piggybacks on this same loop
    rather than its own separate sweep, since this is already exactly
    "a week whose games just changed FINAL-ness" - the NOT EXISTS check
    only needs to run for weeks touched this tick, not every week every
    tick."""
    result = client.query(
        "SELECT DISTINCT w.week_id, w.week_number FROM games g "
        "JOIN weeks w ON w.week_id = g.week_id "
        "WHERE g.status = 'FINAL' AND g.has_final_stats = FALSE"
    )
    for row in result.results:
        load_game_statistics(row["week_number"])
        client.batch(
            [
                (
                    (
                        "UPDATE games SET has_final_stats = TRUE "
                        "WHERE week_id = ? AND status = 'FINAL'"
                    ),
                    [row["week_id"]],
                ),
                (
                    (
                        "UPDATE weeks SET is_complete = TRUE WHERE week_id = ? "
                        "AND NOT EXISTS ("
                        "SELECT 1 FROM games WHERE week_id = ? AND status != 'FINAL'"
                        ")"
                    ),
                    [row["week_id"], row["week_id"]],
                ),
            ]
        )


def _run_user_profiles_refresh(client: D1Client) -> None:
    """Recompute + rewrite every active user's user:{user_id}:season:{season}
    KV key on its own cadence (USER_PROFILES_INTERVAL_SECONDS), unconditional
    - live or quiet - so it isn't suppressed by an ongoing game the way the
    quiet-only tasks are."""
    if not _should_run(
        client, "user_profiles_last_write_at", USER_PROFILES_INTERVAL_SECONDS
    ):
        return

    write_user_profiles()
    _set_state(client, "user_profiles_last_write_at", _now_iso())


def main() -> None:
    client = D1Client(**get_d1_config())
    now = datetime.now(UTC)

    if _is_live_window_active(client):
        _run_live_updates(client)
    else:
        _run_quiet_period_tasks(client)

    _run_pre_kickoff_odds_capture(client, now)
    _run_pregame_weather_capture(client, now)
    _run_deadline_sweep(client, now)
    # Must run before _run_finished_game_stats() marks a week is_complete -
    # this call's own query reads is_complete as of *before* that update, so
    # the exact tick a week's last game goes FINAL still gets one final KV
    # write for it (games.status/score themselves are already fresh by here,
    # from _run_live_updates()/_run_quiet_period_tasks() above - only
    # is_complete's flip timing matters for this ordering).
    write_incomplete_weeks_games()
    _run_finished_game_stats(client)
    # Unconditional, not just from inside the CBS live branch - is_current
    # can flip to a new week (housekeeping runs daily, independent of
    # live/quiet state) days before that week's first game goes live and
    # the CBS branch would otherwise get a chance to write its leaderboard
    # key for the first time, leaving it simply missing from KV until then.
    write_current_week_leaderboard()
    write_current_week_trends()
    write_season_trends()
    _run_user_profiles_refresh(client)
    write_admin_status()


if __name__ == "__main__":
    configure_logging()
    if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"):
        sys.exit(1)
    main()
