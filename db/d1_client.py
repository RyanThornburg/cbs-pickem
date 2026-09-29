"""Client for Cloudflare D1's HTTP query API.

Used identically for every environment (local dev and production) - the only
difference between them is which account_id/database_id/api_token
config.get_d1_config() resolves to. There is no separate local-only code path.
"""

from dataclasses import dataclass, field
from typing import Any

import requests

D1_API_BASE = "https://api.cloudflare.com/client/v4"
# without one a hung connection hangs the whole cron tick indefinitely -
# generous, since a large batch (a week of player stats) can take a while
TIMEOUT_SECONDS = 60


class D1Error(RuntimeError):
    """Raised when a D1 query fails (non-2xx response or success=False)."""


@dataclass
class D1QueryResult:
    """Class for D1 query results"""

    results: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def _bind_params(params: list[Any] | None) -> list[Any]:
    """D1 binds a JSON true/false as the literal text 'true'/'false'"""
    return [int(p) if isinstance(p, bool) else p for p in (params or [])]


class D1Client:
    """Class for D1 client query / batching"""

    def __init__(self, account_id: str, database_id: str, api_token: str):
        self._url = (
            f"{D1_API_BASE}/accounts/{account_id}/d1/database/{database_id}/query"
        )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )

    def query(self, sql: str, params: list[Any] | None = None) -> D1QueryResult:
        """Run a single statement (SELECT/INSERT/UPDATE/DELETE)."""
        return self.batch([(sql, params)])[0]

    def batch(
        self, statements: list[tuple[str, list[Any] | None]]
    ) -> list[D1QueryResult]:
        """Run multiple statements as a single atomic transaction.

        Every call enables foreign keys (`PRAGMA foreign_keys = ON`) as part
        of the same batch, before the caller's statements - D1 already
        enforces them by default (see db/CLAUDE.md), so this is belt and
        braces rather than required.
        """
        payload = {
            "batch": [{"sql": "PRAGMA foreign_keys = ON;", "params": []}]
            + [
                {"sql": sql, "params": _bind_params(params)}
                for sql, params in statements
            ]
        }
        response = self._session.post(self._url, json=payload, timeout=TIMEOUT_SECONDS)
        # D1 puts its own reason (bad SQL, a constraint) in the body of a
        # 4xx, so read that before falling back to raise_for_status()
        try:
            data: dict[str, Any] = response.json()
        except ValueError:
            response.raise_for_status()
            raise

        if not response.ok or not data.get("success"):
            raise D1Error(f"HTTP {response.status_code}: {data.get('errors')}")

        results: list[dict[str, Any]] = data["result"][1:]
        query_results: list[D1QueryResult] = []
        for item in results:
            item_results: list[dict[str, Any]] = item.get("results") or []
            item_meta: dict[str, Any] = item.get("meta") or {}
            query_results.append(D1QueryResult(results=item_results, meta=item_meta))
        return query_results
