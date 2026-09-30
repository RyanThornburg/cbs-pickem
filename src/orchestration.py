"""Recurring, trigger-based scheduling - one stateless tick per invocation.

Meant to be invoked by cron every minute (`* * * * *`); most ticks do almost
nothing (a couple of cheap local D1 checks)

Usage: uv run python -m src.orchestration [local|prod]
"""

import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from config.config import configure_logging, load_env
from db.clients import get_d1
from db.d1_client import D1Client
from src.game_rules import DONE_STATUSES, LIVE_WINDOW_HOURS, sql_list
from src.kv_writer import (
    write_admin_status,
    write_current_week_games,
    write_current_week_leaderboard,
    write_current_week_odds,
    write_current_week_trends,
    write_game_details,
    write_incomplete_weeks_games,
    write_meta_current,
    write_recent_weeks_recap,
    write_season_trends,
    write_user_profiles,
)
from src.loaders.cbs_loader import load_cbs_games, load_cbs_user_picks, load_cbs_weeks
from src.loaders.espn_loader import load_espn_games
from src.loaders.odds_loader import load_the_odds_api_odds
from src.loaders.player_stats_loader import (
    load_live_player_stats,
    load_week_player_stats,
)
from src.loaders.pregame_weather_loader import load_pregame_weather
from src.loaders.scoring_plays_loader import load_scoring_plays
from src.loaders.sports_io_loader import (
    load_game_statistics,
    load_games_data,
    load_live_game_statistics,
)
from src.loaders.teams_loader import main as load_teams
from src.loaders.win_probability_loader import load_final_win_probability
from src.scheduling import (
    acquire_lock,
    get_state,
    run_and_record,
    run_on_interval,
    set_state,
    should_run,
    soft,
)
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

SPORTS_IO_LIVE_INTERVAL_SECONDS = 60
CBS_LIVE_INTERVAL_SECONDS = 120
# game_snapshots aren't captured here - src/live_ticker.py polls ESPN for
# them every 15 seconds, as its own cron process
LIVE_GAME_STATS_INTERVAL_SECONDS = 3 * 60
# ~1 Sports IO call per live game per minute - roughly 2,000 on a full
# Sunday, well inside the 7,500/day Pro quota
LIVE_PLAYER_STATS_INTERVAL_SECONDS = 60
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
# Weekly recap (week:{season}:{weekNN}:recap) - a rotating infographic,
# not a live number, so a few minutes behind a final score is fine and it
# saves a KV write on most ticks
RECAP_INTERVAL_SECONDS = 5 * 60
# After a failure, the deadline sweep and the finished-game stats catch-up
# wait this long before trying again - both are otherwise retried every
# tick until they succeed, and each attempt is a burst of CBS/Sports IO
# calls (CBS logins in particular risk its lockout defenses)
FAILURE_RETRY_SECONDS = 10 * 60


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
    now = utc_iso()
    cutoff = utc_iso(datetime.now(UTC) - timedelta(hours=LIVE_WINDOW_HOURS))
    result = client.query(
        "SELECT 1 FROM games WHERE game_time <= ? AND game_time >= ? "
        f"AND (status IS NULL OR status NOT IN {sql_list(DONE_STATUSES)}) LIMIT 1",
        [now, cutoff],
    )
    return bool(result.results)


def _run_live_updates(client: D1Client) -> None:
    # Sports IO has sent malformed/unexpected game data mid-slate
    run_on_interval(
        client,
        "sports_io_live_poll",
        lambda: load_games_data(live=True),
        "sports_io_live_last_poll_at",
        "sports_io_live_last_success_at",
        SPORTS_IO_LIVE_INTERVAL_SECONDS,
    )

    # Runs for the whole live window, not just pre-deadline - most games kick
    # off at/after the Sunday 1PM ET deadline, and that's also when CBS
    # reveals every entry's picks (not just early-game ones), so this is when
    # the bulk of live pick-grading actually happens via trending status.
    # write_current_week_leaderboard() isn't needed here - main() calls it
    # every tick regardless of branch
    run_on_interval(
        client,
        "cbs_live_poll",
        load_cbs_user_picks,
        "cbs_live_last_poll_at",
        "cbs_live_last_success_at",
        CBS_LIVE_INTERVAL_SECONDS,
    )

    run_on_interval(
        client,
        "live_game_stats",
        lambda: _write_game_details(client, load_live_game_statistics()),
        "live_game_stats_last_capture_at",
        "live_game_stats_last_success_at",
        LIVE_GAME_STATS_INTERVAL_SECONDS,
    )

    run_on_interval(
        client,
        "live_player_stats",
        lambda: _write_game_details(client, load_live_player_stats()),
        "live_player_stats_last_capture_at",
        "live_player_stats_last_success_at",
        LIVE_PLAYER_STATS_INTERVAL_SECONDS,
    )


def _write_game_details(client: D1Client, game_ids: set[int]) -> None:
    """game:{season}:{game_id}:details for games whose box score, player
    stats or win probability just changed. A KV failure here is recorded,
    not raised - the D1 write it follows already happened, and the next
    change rewrites the key anyway."""
    soft(client, "game_details_write", lambda: write_game_details(game_ids))


def _run_win_probability_capture(client: D1Client) -> None:
    """Every tick - load_final_win_probability() only calls ESPN for FINAL
    games that don't have a curve yet (one D1 query otherwise). ESPN is
    undocumented, so a failure is recorded, never raised."""
    run_and_record(
        client,
        "win_probability_capture",
        lambda: _write_game_details(client, load_final_win_probability()),
        "win_probability_last_run_at",
        "win_probability_last_success_at",
    )


def _capture_odds(client: D1Client, state_key: str, success_key: str) -> None:
    """Odds aren't required/shouldn't block, don't raise but log error.
    state_key is the scheduling cursor, success_key only moves when the
    capture worked - see run_and_record()."""

    def capture() -> None:
        load_the_odds_api_odds()
        write_current_week_odds()

    run_and_record(client, "odds_capture", capture, state_key, success_key)


def _run_scoring_plays_refresh(client: D1Client) -> None:
    """Every tick, live window or not - load_scoring_plays() only calls
    Sports IO for games whose score moved since their last fetch (one D1
    query otherwise), and a game's last score can land after the live
    window closes. Enrichment, so a failure is recorded, never raised."""
    run_and_record(
        client,
        "scoring_plays_refresh",
        load_scoring_plays,
        "scoring_plays_last_run_at",
        "scoring_plays_last_success_at",
    )


def _run_quiet_period_tasks(client: D1Client) -> None:
    if should_run(client, "odds_last_call_at", ODDS_INTERVAL_SECONDS):
        _capture_odds(client, "odds_last_call_at", "odds_last_success_at")

    run_on_interval(
        client,
        "cbs_picks_quiet_poll",
        load_cbs_user_picks,
        "cbs_picks_quiet_last_poll_at",
        "cbs_picks_quiet_last_success_at",
        CBS_PICKS_QUIET_INTERVAL_SECONDS,
    )

    run_on_interval(
        client,
        "housekeeping",
        _housekeeping,
        "housekeeping_last_run_at",
        "housekeeping_last_success_at",
        HOUSEKEEPING_INTERVAL_SECONDS,
    )


def _housekeeping() -> None:
    """Daily refresh. Each step is independent, so one failing doesn't skip
    the rest - but any failure still raises at the end, so housekeeping as a
    whole isn't recorded as a success."""
    steps: list[tuple[str, Callable[[], object]]] = [
        ("load_games_data", load_games_data),  # full schedule/weeks refresh
        ("load_teams", load_teams),  # team win/loss/tie records
        ("load_cbs_weeks", load_cbs_weeks),
        ("load_cbs_games", load_cbs_games),
        ("load_espn_games", load_espn_games),  # neutral_site, incomplete weeks
        ("write_meta_current", write_meta_current),
        # load_cbs_weeks() is what moves is_current to a new week, and the
        # odds key is otherwise only written after an odds capture - which
        # runs earlier in the tick, so it can't see the new week yet
        ("write_current_week_odds", write_current_week_odds),
        # future weeks' games keys - the per-tick write in main() only
        # covers weeks that have started
        ("write_future_weeks_games", lambda: write_incomplete_weeks_games(True)),
    ]
    failed: list[str] = []
    for name, step in steps:
        try:
            step()
        except Exception:
            logger.exception("Housekeeping step %s failed - continuing", name)
            failed.append(name)
    if failed:
        raise RuntimeError(f"Housekeeping steps failed: {', '.join(failed)}")


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
    if not should_run(
        client, "odds_prekickoff_last_call_at", ODDS_PREKICKOFF_MIN_GAP_SECONDS
    ):
        return

    now_str = utc_iso(now)
    lead_cutoff = utc_iso(now + timedelta(minutes=ODDS_PREKICKOFF_LEAD_MINUTES))
    result = client.query(
        "SELECT 1 FROM games WHERE game_time > ? AND game_time <= ? "
        f"AND (status IS NULL OR status NOT IN {sql_list(DONE_STATUSES)}) LIMIT 1",
        [now_str, lead_cutoff],
    )
    if not result.results:
        return

    logger.info("Running pre-kickoff odds capture")
    _capture_odds(
        client, "odds_prekickoff_last_call_at", "odds_prekickoff_last_success_at"
    )


def _run_pregame_weather_capture(client: D1Client, now: datetime) -> None:
    """Pregame forecast for games that haven't started yet - same
    baseline+near-kickoff-boost shape as _run_pre_kickoff_odds_capture(),
    except continuous (weather is worth re-checking repeatedly as it
    changes) rather than a single pre-kickoff pulse. Runs unconditionally
    every tick, live or quiet, so an already-live early game can't
    suppress the refresh for an approaching later one.
    """
    now_str = utc_iso(now)
    near_cutoff = utc_iso(now + timedelta(hours=WEATHER_PREGAME_NEAR_WINDOW_HOURS))
    has_near_kickoff = bool(
        client.query(
            "SELECT 1 FROM games WHERE game_time > ? AND game_time <= ? "
            "AND status = 'SCHEDULED' LIMIT 1",
            [now_str, near_cutoff],
        ).results
    )
    interval = (
        WEATHER_PREGAME_NEAR_INTERVAL_SECONDS
        if has_near_kickoff
        else WEATHER_PREGAME_BASELINE_INTERVAL_SECONDS
    )
    if not should_run(client, "weather_pregame_last_capture_at", interval):
        return

    run_and_record(
        client,
        "pregame_weather_capture",
        load_pregame_weather,
        "weather_pregame_last_capture_at",
        "weather_pregame_last_success_at",
    )


def _run_deadline_sweep(client: D1Client, now: datetime) -> None:
    deadline = _current_week_deadline_utc(now)
    sunday_date = deadline.date().isoformat()
    if now < deadline:
        return
    if get_state(client, "deadline_last_synced_sunday") == sunday_date:
        return
    # a failed sweep is retried, but not every tick - see FAILURE_RETRY_SECONDS
    if not should_run(client, "deadline_sweep_last_attempt_at", FAILURE_RETRY_SECONDS):
        return
    set_state(client, "deadline_sweep_last_attempt_at", utc_iso())

    def sweep() -> None:
        load_cbs_weeks()
        load_cbs_games()
        load_cbs_user_picks()
        write_meta_current()
        write_current_week_games()
        write_current_week_leaderboard()

    if soft(client, "deadline_sweep", sweep):
        set_state(client, "deadline_last_synced_sunday", sunday_date)
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
    # a failed week is retried, but not every tick - see FAILURE_RETRY_SECONDS
    if not should_run(
        client, "finished_game_stats_last_failure_at", FAILURE_RETRY_SECONDS
    ):
        return

    result = client.query(
        "SELECT DISTINCT w.week_id, w.week_number FROM games g "
        "JOIN weeks w ON w.week_id = g.week_id "
        "WHERE g.status = 'FINAL' AND g.has_final_stats = FALSE"
    )
    for row in result.results:
        if not _finish_week_stats(client, row["week_id"], row["week_number"]):
            set_state(client, "finished_game_stats_last_failure_at", utc_iso())


def _finish_week_stats(client: D1Client, week_id: int, week_number: int) -> bool:
    """Final box scores for one week, then mark its FINAL games
    has_final_stats (and the week is_complete once every game is FINAL).
    False, with nothing marked, if the final team stats failed - so the
    week is retried."""
    game_ids: set[int] = set()
    if not soft(
        client,
        "final_game_stats",
        lambda: game_ids.update(load_game_statistics(week_number)),
    ):
        return False
    # final player box scores ride along with the final team stats -
    # soft-fail so has_final_stats below still gets set
    soft(
        client,
        "final_player_stats",
        lambda: game_ids.update(load_week_player_stats(week_number)),
    )
    _write_game_details(client, game_ids)
    client.batch(
        [
            (
                (
                    "UPDATE games SET has_final_stats = TRUE "
                    "WHERE week_id = ? AND status = 'FINAL'"
                ),
                [week_id],
            ),
            (
                (
                    "UPDATE weeks SET is_complete = TRUE WHERE week_id = ? "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM games WHERE week_id = ? AND status != 'FINAL'"
                    ")"
                ),
                [week_id, week_id],
            ),
        ]
    )
    return True


def _run_user_profiles_refresh(client: D1Client) -> None:
    """Recompute + rewrite every active user's user:{user_id}:season:{season}
    KV key on its own cadence (USER_PROFILES_INTERVAL_SECONDS), unconditional
    - live or quiet - so it isn't suppressed by an ongoing game the way the
    quiet-only tasks are."""
    run_on_interval(
        client,
        "user_profiles_write",
        write_user_profiles,
        "user_profiles_last_write_at",
        "user_profiles_last_success_at",
        USER_PROFILES_INTERVAL_SECONDS,
    )


def _run_recap_refresh(client: D1Client) -> None:
    """Rewrite the recap keys for the current week plus any week still
    in progress or just finished (see write_recent_weeks_recap()) every
    RECAP_INTERVAL_SECONDS, live or quiet."""
    run_on_interval(
        client,
        "recap_write",
        write_recent_weeks_recap,
        "recap_last_write_at",
        "recap_last_success_at",
        RECAP_INTERVAL_SECONDS,
    )


def main() -> None:
    client = get_d1()
    now = datetime.now(UTC)

    if _is_live_window_active(client):
        _run_live_updates(client)
    else:
        _run_quiet_period_tasks(client)

    _run_pre_kickoff_odds_capture(client, now)
    _run_pregame_weather_capture(client, now)
    _run_deadline_sweep(client, now)
    # before the games KV write below, so a new score's play lands the same tick
    _run_scoring_plays_refresh(client)
    # Must run before _run_finished_game_stats() marks a week is_complete -
    # this call's own query reads is_complete as of *before* that update, so
    # the exact tick a week's last game goes FINAL still gets one final KV
    # write for it (games.status/score themselves are already fresh by here,
    # from _run_live_updates()/_run_quiet_period_tasks() above - only
    # is_complete's flip timing matters for this ordering).
    soft(client, "games_kv_write", write_incomplete_weeks_games)
    _run_finished_game_stats(client)
    _run_win_probability_capture(client)
    # Unconditional, not just from inside the CBS live branch - is_current
    # can flip to a new week (housekeeping runs daily, independent of
    # live/quiet state) days before that week's first game goes live and
    # the CBS branch would otherwise get a chance to write its leaderboard
    # key for the first time, leaving it simply missing from KV until then.
    soft(client, "leaderboard_kv_write", write_current_week_leaderboard)
    soft(client, "week_trends_kv_write", write_current_week_trends)
    soft(client, "season_trends_kv_write", write_season_trends)
    _run_recap_refresh(client)
    _run_user_profiles_refresh(client)
    # last, so it reflects every failure recorded above
    soft(client, "admin_kv_write", write_admin_status)


if __name__ == "__main__":
    configure_logging()
    env = sys.argv[1] if len(sys.argv) > 1 else "local"
    if not load_env(env):
        sys.exit(1)
    tick_lock = acquire_lock(f"orchestration.{env}")
    if tick_lock is None:
        logger.warning("Previous %s tick still running - skipping this one", env)
        sys.exit(0)
    main()
