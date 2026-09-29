"""db/schema.sql applies cleanly through db.setup's own statement splitter,
and re-applies cleanly on top of itself (every CREATE uses IF NOT EXISTS)."""

from config.config import SCHEMA_PATH
from db.setup import _split_statements
from tests.conftest import FakeD1


def _names(d1: FakeD1, kind: str) -> set[str]:
    rows = d1.query(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'",
        [kind],
    ).results
    return {row["name"] for row in rows}


def test_core_tables_exist(d1: FakeD1) -> None:
    assert {
        "users",
        "teams",
        "stadiums",
        "seasons",
        "weeks",
        "games",
        "user_picks",
        "weekly_performance",
        "historical_standings",
        "historical_user_mapping",
        "odds_snapshots",
        "game_snapshots",
        "mapping_gaps",
        "system_events",
    } <= _names(d1, "table")


def test_schema_reapplies_cleanly(d1: FakeD1) -> None:
    tables_before = _names(d1, "table")
    d1.batch([(stmt, None) for stmt in _split_statements(SCHEMA_PATH.read_text())])
    assert _names(d1, "table") == tables_before


def test_splitter_keeps_trigger_bodies_whole() -> None:
    statements = _split_statements(SCHEMA_PATH.read_text())
    triggers = [s for s in statements if s.upper().startswith("CREATE TRIGGER")]
    assert triggers
    for trigger in triggers:
        assert trigger.rstrip().upper().endswith("END")


def test_games_forecast_columns_exist(d1: FakeD1) -> None:
    # both were silently broken in schema.sql once (a swallowed column and
    # a missing comma), see CLAUDE.local.md's weather icon entry
    columns = {row["name"] for row in d1.query("PRAGMA table_info(games)").results}
    assert {"forecast_temp_f", "forecast_captured_at", "forecast_icon"} <= columns


def test_weekly_performance_updated_at_trigger(seed, d1: FakeD1) -> None:
    week_id = seed.week(1)
    user_id = seed.user("a")
    perf_id = seed.performance(user_id, week_id, 3)
    d1.query(
        "UPDATE weekly_performance SET updated_at = '2000-01-01' "
        "WHERE performance_id = ?",
        [perf_id],
    )
    row = d1.query(
        "SELECT updated_at FROM weekly_performance WHERE performance_id = ?",
        [perf_id],
    ).results[0]
    # the trigger overwrites whatever the UPDATE set with CURRENT_TIMESTAMP
    assert row["updated_at"] != "2000-01-01"
