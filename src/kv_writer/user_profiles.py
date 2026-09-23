"""user:{user_id}:season:{season} - see src/CLAUDE.md's KV writer section."""

import logging
from datetime import UTC, datetime

from config.config import SEASON, get_d1_config, get_kv_config
from db.d1_client import D1Client
from db.kv_client import KVClient
from src.kv_writer.historical import career_record_by_user
from src.user_stats import compute_user_profiles

logger = logging.getLogger(__name__)


def write_user_profiles() -> None:
    """Write user:{user_id}:season:{season} for every active user - career
    record (years played/titles/best finish, from historical_standings) plus
    this season's streaks/tendencies (hot streak, team-pick streak, home/
    away/favorite/underdog bias, contrarian accuracy, nemesis/lucky team,
    consistency, clutch, head-to-head). All the actual computation lives in
    src/user_stats.py's compute_user_profiles() - this just supplies the
    career data and does the KV writes, one per user (see
    orchestration.py's own cadence for this - it's deliberately not on
    every tick like most other write_* functions here, since a per-user
    KV write for every active user on every minute-cron tick would be a lot
    of avoidable KV write volume for data that only actually changes when
    picks get made/graded)."""
    d1 = D1Client(**get_d1_config())
    career_by_user = career_record_by_user(d1)
    profiles = compute_user_profiles(d1, career_by_user)
    if not profiles:
        logger.warning("No active users found - not writing user profile keys")
        return

    kv = KVClient(**get_kv_config())
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for user_id, profile in profiles.items():
        kv.write(f"user:{user_id}:season:{SEASON}", {**profile, "updated_at": now})

    logger.info("Wrote %d user:*:season:%s profile keys to KV", len(profiles), SEASON)
