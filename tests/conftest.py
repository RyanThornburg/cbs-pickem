"""Shared fixtures: an in-memory SQLite stand-in for D1 with db/schema.sql
applied, plus small seed helpers for building test data on top of it.

D1 is SQLite, so running the real SQL here tests the queries themselves,
not just the Python around them."""

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from config.config import SCHEMA_PATH, SEASON
from db.d1_client import D1QueryResult, _bind_params
from db.setup import _split_statements


class FakeD1:
    """Same query()/batch() surface as db.d1_client.D1Client, backed by an
    in-memory SQLite database instead of D1's HTTP API."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def query(self, sql: str, params: list[Any] | None = None) -> D1QueryResult:
        return self.batch([(sql, params)])[0]

    def batch(
        self, statements: list[tuple[str, list[Any] | None]]
    ) -> list[D1QueryResult]:
        # atomic like D1's batch: all statements land or none do
        results: list[D1QueryResult] = []
        self.conn.execute("BEGIN")
        try:
            for sql, params in statements:
                cursor = self.conn.execute(sql, _bind_params(params))
                rows = [dict(row) for row in cursor.fetchall()]
                results.append(
                    D1QueryResult(
                        results=rows,
                        meta={
                            "changes": cursor.rowcount,
                            "last_row_id": cursor.lastrowid,
                        },
                    )
                )
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        return results


class Seed:
    """Inserts the minimum rows each table's foreign keys need. Every
    helper returns the new row's id."""

    def __init__(self, d1: FakeD1) -> None:
        self.d1 = d1
        self._team_count = 0

    def _insert(self, table: str, **values: Any) -> int:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        result = self.d1.query(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            list(values.values()),
        )
        return result.meta["last_row_id"]

    def season(self, season_id: int = SEASON) -> int:
        self.d1.query(
            "INSERT OR IGNORE INTO seasons (season_id, name) VALUES (?, ?)",
            [season_id, str(season_id)],
        )
        return season_id

    def week(self, week_number: int, season_id: int = SEASON) -> int:
        self.season(season_id)
        return self._insert(
            "weeks",
            season_id=season_id,
            week_number=week_number,
            name=f"Week {week_number}",
        )

    def user(self, name: str, is_active: bool = True) -> int:
        return self._insert("users", name=name, is_active=is_active)

    def team(self, abbreviation: str | None = None) -> int:
        self._team_count += 1
        abbreviation = abbreviation or f"T{self._team_count}"
        return self._insert(
            "teams", name=abbreviation, season=SEASON, abbreviation=abbreviation
        )

    def game(self, week_id: int, **values: Any) -> int:
        values.setdefault("home_team_id", self.team())
        values.setdefault("away_team_id", self.team())
        return self._insert("games", week_id=week_id, **values)

    def performance(
        self,
        user_id: int,
        week_id: int,
        picks_correct: int | None,
        **values: Any,
    ) -> int:
        values.setdefault("has_submitted_picks", True)
        values.setdefault("picks_made", 5)
        return self._insert(
            "weekly_performance",
            user_id=user_id,
            week_id=week_id,
            picks_correct=picks_correct,
            **values,
        )

    def pick(
        self, user_id: int, game_id: int, picked_team_id: int, **values: Any
    ) -> int:
        return self._insert(
            "user_picks",
            user_id=user_id,
            game_id=game_id,
            picked_team_id=picked_team_id,
            **values,
        )

    def historical(
        self, user_id: int, season_id: int, final_rank: int = 1, final_score: int = 0
    ) -> int:
        self.season(season_id)
        return self._insert(
            "historical_standings",
            user_id=user_id,
            season_id=season_id,
            final_rank=final_rank,
            final_score=final_score,
        )


@pytest.fixture
def d1() -> Iterator[FakeD1]:
    fake = FakeD1()
    fake.batch([(stmt, None) for stmt in _split_statements(SCHEMA_PATH.read_text())])
    yield fake
    fake.conn.close()


@pytest.fixture
def seed(d1: FakeD1) -> Seed:
    return Seed(d1)
