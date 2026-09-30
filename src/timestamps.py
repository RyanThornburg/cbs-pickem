"""The ISO8601 UTC text format every stored/served timestamp uses
(games.game_time, orchestration_state cursors, KV updated_at, ...). Plain
text so D1 can compare them as strings."""

from datetime import UTC, datetime

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_iso(dt: datetime | None = None) -> str:
    """dt (a UTC datetime, default now) as ISO8601 UTC text"""
    return (dt or datetime.now(UTC)).strftime(ISO_FORMAT)


def parse_utc_iso(value: str) -> datetime:
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=UTC)
