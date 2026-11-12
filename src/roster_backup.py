"""Back up the UI's roster:{season} KV key to D1.

Usage: uv run python -m src.roster_backup [local|prod]

roster:{season} is the one KV key this repo never writes - the admin page
edits it (entry fee payments). KV has no history, so daily housekeeping
saves a copy to roster_backups whenever it changed, and season close-out
records each user's paid amount from it. See src/CLAUDE.md's "Roster
backup" section for the shape it reads.
"""

import json
import logging
from typing import Any

from config.config import SEASON, run_cli
from db.clients import get_d1, get_kv
from src.timestamps import utc_iso

logger = logging.getLogger(__name__)

_LATEST_SQL = """
SELECT value_json FROM roster_backups
WHERE season_id = ?
ORDER BY roster_backup_id DESC
LIMIT 1
"""

_INSERT_SQL = """
INSERT INTO roster_backups (season_id, captured_at, value_json) VALUES (?, ?, ?)
"""


def roster_key(season: int = SEASON) -> str:
    return f"roster:{season}"


def backup_roster() -> dict[str, Any] | None:
    """Save roster:{SEASON} to roster_backups if it differs from the last
    copy, and return it. None (and nothing saved) while the key doesn't
    exist, so a missing key never looks like an emptied roster."""
    roster = get_kv().read(roster_key())
    if roster is None:
        logger.info("No %s in KV yet - nothing to back up", roster_key())
        return None

    d1 = get_d1()
    value_json = json.dumps(roster, sort_keys=True)
    latest = d1.query(_LATEST_SQL, [SEASON]).results
    if latest and latest[0]["value_json"] == value_json:
        logger.info("%s unchanged since the last backup", roster_key())
        return roster

    d1.query(_INSERT_SQL, [SEASON, utc_iso(), value_json])
    logger.info("Backed up %s to roster_backups", roster_key())
    return roster


def paid_amounts(roster: dict[str, Any] | None) -> dict[int, int | float]:
    """user_id -> paid_amount from a roster value. Users with no
    paid_amount, or an entry that isn't the expected shape, are left out
    (logged), not guessed."""
    paid: dict[int, int | float] = {}
    for user_id, entry in ((roster or {}).get("users") or {}).items():
        amount = entry.get("paid_amount") if isinstance(entry, dict) else None
        if amount is None:
            continue
        try:
            paid[int(user_id)] = amount
        except ValueError:
            logger.warning("Roster entry %r isn't a user_id - skipped", user_id)
    return paid


def paid_payouts(roster: dict[str, Any] | None) -> dict[str, dict[int, int | float]]:
    """period_key -> user_id -> paid_amount from a roster value's payouts
    (prize money paid out, dollars and cents). A winner with no entry is
    left out - unknown, not 0, since the admin page's "Mark unpaid"
    deletes the entry. Anything not the expected shape is left out too
    (logged)."""
    paid: dict[str, dict[int, int | float]] = {}
    for period_key, winners in ((roster or {}).get("payouts") or {}).items():
        if not isinstance(winners, dict):
            logger.warning("Roster payouts.%s isn't an object - skipped", period_key)
            continue
        amounts = paid_amounts({"users": winners})
        if amounts:
            paid[period_key] = amounts
    return paid


def main() -> None:
    backup_roster()


if __name__ == "__main__":
    run_cli(main)
