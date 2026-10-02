"""src/live_ticker.py's 15-second loop, and the Cloudflare D1/KV HTTP
clients every read and write goes through (db/d1_client.py,
db/kv_client.py)."""

from typing import Any

import pytest
import requests

from db import d1_client, kv_client
from db.d1_client import D1Client, D1Error
from db.kv_client import KVClient, KVError
from src import live_ticker
from src.scheduling import get_state
from tests.conftest import Clients, FakeD1


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestLiveTicker:
    @pytest.fixture
    def ticker(
        self, clients: Clients, monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, Any]:
        clock = FakeClock()
        monkeypatch.setattr(live_ticker.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(live_ticker.time, "sleep", clock.sleep)
        state: dict[str, Any] = {
            "clock": clock,
            "candidates": True,
            "rounds": [],  # what each snapshot round returns (changed game ids)
            "writes": [],
            "details": [],
        }

        def snapshots() -> set[int]:
            result = state["rounds"].pop(0) if state["rounds"] else set()
            if isinstance(result, Exception):
                raise result
            clock.now += 2  # a round takes a moment
            return result

        monkeypatch.setattr(
            live_ticker, "has_candidate_games", lambda: state["candidates"]
        )
        monkeypatch.setattr(live_ticker, "load_game_snapshots", snapshots)
        monkeypatch.setattr(
            live_ticker,
            "write_games_weeks",
            lambda ids: state["writes"].append(set(ids)),
        )
        monkeypatch.setattr(
            live_ticker,
            "write_game_details",
            lambda ids: state["details"].append(set(ids)),
        )
        return state

    def test_four_rounds_on_the_quarter_minute(
        self, ticker: dict[str, Any], d1: FakeD1
    ) -> None:
        ticker["rounds"] = [{1}, set(), {1, 2}, set()]

        live_ticker.main()

        clock = ticker["clock"]
        assert clock.now - 1000.0 < 60  # done before cron starts the next run
        assert ticker["rounds"] == []  # all four ran
        assert clock.slept == [13, 13, 13]  # 15s apart, less each round's 2s
        # the games key is rewritten only when a snapshot changed
        assert ticker["writes"] == [{1}, {1, 2}]
        # and the changed games' details keys (live win probability)
        assert ticker["details"] == [{1}, {1, 2}]
        assert get_state(d1, "game_snapshot_last_success_at") is not None

    def test_outside_game_time(self, ticker: dict[str, Any], d1: FakeD1) -> None:
        ticker["candidates"] = False
        ticker["rounds"] = [{1}]

        live_ticker.main()

        assert ticker["rounds"] == [{1}]  # never polled
        assert get_state(d1, "game_snapshot_last_capture_at") is None

    def test_a_failed_round_doesnt_stop_the_next(
        self, ticker: dict[str, Any], d1: FakeD1
    ) -> None:
        ticker["rounds"] = [RuntimeError("ESPN down"), {3}, set(), set()]

        live_ticker.main()

        assert ticker["writes"] == [{3}]
        events = d1.query("SELECT source, occurrences FROM system_events").results
        assert events == [{"source": "game_snapshot_capture", "occurrences": 1}]

    def test_kv_failure_is_recorded(
        self, ticker: dict[str, Any], d1: FakeD1, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(ids: set[int]) -> None:
            raise RuntimeError("KV 429")

        monkeypatch.setattr(live_ticker, "write_games_weeks", fail)
        ticker["rounds"] = [{1}, set(), set(), set()]

        live_ticker.main()

        sources = [
            r["source"] for r in d1.query("SELECT source FROM system_events").results
        ]
        assert sources == ["live_games_kv_write"]
        # the details write still ran
        assert ticker["details"] == [{1}]


class FakeResponse:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self.ok = status < 400
        self._body = body

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    """requests.Session stand-in: queued responses, recorded requests"""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.responses: list[FakeResponse] = []
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def _next(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("PUT", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("GET", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._next("DELETE", url, **kwargs)


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    fake = FakeSession()
    monkeypatch.setattr(d1_client.requests, "Session", lambda: fake)
    monkeypatch.setattr(kv_client.requests, "Session", lambda: fake)
    return fake


class TestD1Client:
    def _d1(self) -> D1Client:
        return D1Client("acct", "db", "token")

    def test_batch(self, session: FakeSession) -> None:
        session.responses.append(
            FakeResponse(
                200,
                {
                    "success": True,
                    "result": [
                        {"results": []},  # the PRAGMA
                        {"results": [{"n": 1}], "meta": {"changes": 0}},
                        {"results": None, "meta": {"changes": 2}},
                    ],
                },
            )
        )

        results = self._d1().batch(
            [("SELECT 1 AS n", None), ("UPDATE t SET a = ?", [True])]
        )

        _, url, kwargs = session.requests[0]
        assert url.endswith("/accounts/acct/d1/database/db/query")
        assert session.headers["Authorization"] == "Bearer token"
        batch = kwargs["json"]["batch"]
        assert batch[0]["sql"] == "PRAGMA foreign_keys = ON;"
        # D1 would store a JSON true as the text 'true'
        assert batch[2]["params"] == [1]
        assert (
            type(batch[2]["params"][0]) is int
        )  # True == 1 in Python, so check the type
        assert kwargs["timeout"] == d1_client.TIMEOUT_SECONDS
        assert [r.results for r in results] == [[{"n": 1}], []]
        assert results[1].meta == {"changes": 2}

    def test_query_returns_one_result(self, session: FakeSession) -> None:
        session.responses.append(
            FakeResponse(
                200, {"success": True, "result": [{}, {"results": [{"a": 1}]}]}
            )
        )
        assert self._d1().query("SELECT 1 AS a").results == [{"a": 1}]

    def test_error_carries_d1s_own_message(self, session: FakeSession) -> None:
        session.responses.append(
            FakeResponse(
                400,
                {"success": False, "errors": [{"message": "UNIQUE constraint failed"}]},
            )
        )
        with pytest.raises(D1Error, match="UNIQUE constraint failed"):
            self._d1().query("INSERT ...")

    def test_success_false_on_a_200(self, session: FakeSession) -> None:
        session.responses.append(
            FakeResponse(200, {"success": False, "errors": ["nope"]})
        )
        with pytest.raises(D1Error):
            self._d1().query("SELECT 1")

    def test_non_json_error(self, session: FakeSession) -> None:
        session.responses.append(FakeResponse(502))
        with pytest.raises(requests.HTTPError):
            self._d1().query("SELECT 1")


class TestKVClient:
    def _kv(self) -> KVClient:
        return KVClient("acct", "ns", "token")

    def test_write(self, session: FakeSession) -> None:
        session.responses.append(FakeResponse(200, {"success": True}))

        self._kv().write("week:2026:03:games", {"a": 1})

        method, url, kwargs = session.requests[0]
        assert method == "PUT"
        # the key is URL-encoded, colons included
        assert url.endswith("/namespaces/ns/values/week%3A2026%3A03%3Agames")
        assert kwargs["data"] == b'{"a": 1}'

    def test_rate_limited_write_retries_once(
        self, session: FakeSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the live ticker and the minute tick can write the same key in one second
        sleeps: list[float] = []
        monkeypatch.setattr(kv_client.time, "sleep", sleeps.append)
        session.responses += [FakeResponse(429), FakeResponse(200, {"success": True})]

        self._kv().write("k", {})

        assert sleeps == [kv_client.RATE_LIMIT_RETRY_SECONDS]
        assert len(session.requests) == 2

    def test_second_429_raises(
        self, session: FakeSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(kv_client.time, "sleep", lambda s: None)
        session.responses += [FakeResponse(429), FakeResponse(429)]
        with pytest.raises(requests.HTTPError):
            self._kv().write("k", {})

    def test_unsuccessful_write(self, session: FakeSession) -> None:
        session.responses.append(
            FakeResponse(200, {"success": False, "errors": ["bad"]})
        )
        with pytest.raises(KVError):
            self._kv().write("k", {})

    def test_read(self, session: FakeSession) -> None:
        session.responses += [FakeResponse(200, {"a": 1}), FakeResponse(404)]
        kv = self._kv()
        assert kv.read("k") == {"a": 1}
        assert kv.read("missing") is None

    def test_delete(self, session: FakeSession) -> None:
        session.responses.append(FakeResponse(200, {"success": True}))
        self._kv().delete("old:key")
        assert session.requests[0][0] == "DELETE"
