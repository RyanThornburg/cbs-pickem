"""Fast live refresh - a game_snapshots capture from ESPN every 15 seconds
during games, and a rewrite of the games KV key for any week whose
snapshot changed.

src/orchestration.py only ticks once a minute, and that tick can take 40+
seconds, so the scoreboard was routinely a minute or more behind ESPN's
clock. This runs as its own cron entry next to orchestration (also every
minute), loops for most of its minute, and holds its own lock so a slow
run is never doubled up. ESPN's scoreboard is one call for every game and
has no quota; nothing here touches Sports IO, CBS or The Odds API.

Outside game time it exits after one D1 query.

Usage (cron, every minute): uv run python -m src.live_ticker [local|prod]
"""

import logging
import sys
import time

from config.config import configure_logging, get_d1_config, load_env
from db.d1_client import D1Client
from src.kv_writer import write_games_weeks
from src.loaders.game_snapshots_loader import has_candidate_games, load_game_snapshots
from src.scheduling import acquire_lock, now_iso, set_state, soft

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 15
# no new round starts after this many seconds into the run, so each run
# finishes before cron starts the next one - rounds land at 0/15/30/45s
LAST_ROUND_START_SECONDS = 45


def _round(client: D1Client) -> None:
    """One capture: new snapshots, then the games key for just the weeks
    that changed. Cursor/success keys match what orchestration used to set
    for snapshots, so meta:admin's game_snapshot_capture entry still works."""
    changed: set[int] = set()
    if soft(
        client,
        "game_snapshot_capture",
        lambda: changed.update(load_game_snapshots()),
    ):
        set_state(client, "game_snapshot_last_success_at", now_iso())
    set_state(client, "game_snapshot_last_capture_at", now_iso())

    if changed:
        soft(client, "live_games_kv_write", lambda: write_games_weeks(changed))


def main() -> None:
    if not has_candidate_games():
        return

    client = D1Client(**get_d1_config())
    started = time.monotonic()
    next_round = 0.0
    while next_round <= LAST_ROUND_START_SECONDS:
        wait = started + next_round - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _round(client)
        next_round += INTERVAL_SECONDS


if __name__ == "__main__":
    configure_logging()
    env = sys.argv[1] if len(sys.argv) > 1 else "local"
    if not load_env(env):
        sys.exit(1)
    run_lock = acquire_lock(f"live_ticker.{env}")
    if run_lock is None:
        logger.warning("Previous %s live ticker run still going - skipping", env)
        sys.exit(0)
    main()
