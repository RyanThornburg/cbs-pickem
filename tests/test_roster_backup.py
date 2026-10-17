"""src/roster_backup.py - copies of the UI's roster:{season} KV key."""

from datetime import UTC, datetime, timedelta

import pytest

from config.config import SEASON
from src import roster_backup
from tests.conftest import Clients, Seed, freeze_all

NOW = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)
KEY = f"roster:{SEASON}"


def _backups(clients: Clients) -> list[dict]:
    return clients.d1.query(
        "SELECT captured_at, value_json FROM roster_backups ORDER BY roster_backup_id"
    ).results


@pytest.fixture(autouse=True)
def _season(seed: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    seed.season()
    freeze_all(monkeypatch, NOW)


def test_no_key_saves_nothing(clients: Clients) -> None:
    assert roster_backup.backup_roster() is None
    assert _backups(clients) == []


def test_saves_only_when_changed(
    clients: Clients, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients.kv.values[KEY] = {"users": {"1": {"paid_amount": 200}}}
    roster_backup.backup_roster()
    freeze_all(monkeypatch, NOW + timedelta(days=1))
    roster_backup.backup_roster()  # unchanged
    assert len(_backups(clients)) == 1

    clients.kv.values[KEY] = {"users": {"1": {"paid_amount": 200}, "2": {}}}
    freeze_all(monkeypatch, NOW + timedelta(days=2))
    assert roster_backup.backup_roster() == clients.kv.values[KEY]
    backups = _backups(clients)
    assert len(backups) == 2
    assert backups[1]["captured_at"] == "2026-10-02T16:00:00Z"


def test_paid_amounts_skips_what_it_cant_read() -> None:
    roster = {
        "users": {
            "1": {"paid_amount": 200},
            "2": {"paid_amount": None, "note": "owes"},
            "3": "not an entry",
            "bob": {"paid_amount": 50},
        }
    }
    assert roster_backup.paid_amounts(roster) == {1: 200}
    assert roster_backup.paid_amounts(None) == {}
