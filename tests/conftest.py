"""Shared fixtures: an in-memory SQLite stand-in for D1 with db/schema.sql
applied, plus small seed helpers for building test data on top of it.

D1 is SQLite, so running the real SQL here tests the queries themselves,
not just the Python around them."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import pytest

from api import sports_io_client, the_odds_api_client, weather_api
from config.config import SCHEMA_PATH, SEASON
from db import clients as db_clients
from db.d1_client import D1QueryResult, _bind_params
from db.setup import _split_statements
from src import timestamps


def iso(dt: datetime) -> str:
    """games.game_time's format - ISO8601 UTC text"""
    return timestamps.utc_iso(dt.astimezone(UTC))


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


class FakeKV:
    """Same write() surface as db.kv_client.KVClient - keeps each key's
    latest value in `values` instead of writing to Cloudflare."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def write(self, key: str, value: dict[str, Any]) -> None:
        self.values[key] = value


class Clients:
    """Points db.clients.get_d1()/get_kv() at the fakes, so every loader and
    write_*() function runs end to end without Cloudflare."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, d1: FakeD1) -> None:
        self.d1 = d1
        self.kv = FakeKV()
        monkeypatch.setattr(db_clients, "D1Client", lambda **_: self.d1)
        monkeypatch.setattr(db_clients, "KVClient", lambda **_: self.kv)
        monkeypatch.setattr(db_clients, "get_d1_config", dict)
        monkeypatch.setattr(db_clients, "get_kv_config", dict)


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

    def team(self, abbreviation: str | None = None, **values: Any) -> int:
        self._team_count += 1
        abbreviation = abbreviation or f"T{self._team_count}"
        values.setdefault("nick_name", abbreviation.title())
        return self._insert(
            "teams",
            name=abbreviation,
            season=SEASON,
            abbreviation=abbreviation,
            **values,
        )

    def game(self, week_id: int, **values: Any) -> int:
        # not setdefault(): that would create a team even when one is given
        for side in ("home_team_id", "away_team_id"):
            if side not in values:
                values[side] = self.team()
        if isinstance(values.get("game_time"), datetime):
            values["game_time"] = iso(values["game_time"])
        return self._insert("games", week_id=week_id, **values)

    def final(
        self,
        week_id: int,
        home: int,
        away: int,
        score: tuple[int, int],
        spread: float | None,
        kickoff: datetime | str,
        **values: Any,
    ) -> int:
        """A FINAL game - `score` is (home, away), `spread` the home line"""
        return self.game(
            week_id,
            home_team_id=home,
            away_team_id=away,
            home_score=score[0],
            away_score=score[1],
            cbs_spread=spread,
            game_time=kickoff,
            status="FINAL",
            **values,
        )

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


@pytest.fixture(autouse=True)
def _fresh_clients() -> Iterator[None]:
    """get_d1()/get_kv() and the API clients are cached per process - drop
    them around every test so one test's fakes never leak into the next"""
    caches = (
        db_clients.get_d1,
        db_clients.get_kv,
        sports_io_client._new_client,
        the_odds_api_client._new_client,
        weather_api._new_client,
    )
    for cached in caches:
        cached.cache_clear()
    yield
    for cached in caches:
        cached.cache_clear()


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch, d1: FakeD1) -> Clients:
    return Clients(monkeypatch, d1)


def freeze(monkeypatch: pytest.MonkeyPatch, module: ModuleType, when: datetime) -> None:
    """Make `module`'s datetime.now() return `when` - for loaders that
    compare saved API data (kickoff times) against the current time. Also
    freezes src.timestamps, where utc_iso() reads the clock."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(module, "datetime", Frozen)
    monkeypatch.setattr(timestamps, "datetime", Frozen)


@pytest.fixture
def apis(monkeypatch: pytest.MonkeyPatch) -> Any:
    """every API client serving the saved responses in tests/fixtures/api/"""
    from tests.api_fixtures import FakeApis

    return FakeApis(monkeypatch)


@pytest.fixture
def loaders(clients: Clients) -> Clients:
    """every loader module writing to the fake D1 - the same fakes as
    `clients`, kept as its own name for the loader tests"""
    return clients


@pytest.fixture
def season(apis: Any, loaders: Clients, seed: Seed) -> FakeD1:
    """the database as the season bootstrap and a daily sync leave it:
    stadiums, teams and the saved Sports IO schedule, run through the real
    loaders"""
    from src.loaders import sports_io_loader, stadiums_loader, teams_loader

    seed.season()
    stadiums_loader.load_stadiums()
    teams_loader.main()
    sports_io_loader.load_games_data()
    return loaders.d1


def freeze_all(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    """freeze() every loaded project module that uses datetime.now() - for
    an orchestration tick, which reaches most of them. Call again to move
    the clock."""
    import sys

    for name, module in list(sys.modules.items()):
        found = getattr(module, "datetime", None)
        if (
            name.split(".")[0] in ("src", "api", "db")
            and isinstance(found, type)
            and issubclass(found, datetime)
        ):
            freeze(monkeypatch, module, when)
