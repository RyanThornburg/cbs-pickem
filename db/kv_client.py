"""Client for Cloudflare KV's HTTP storage API.

Used identically for every environment (local dev and production) - the only
difference between them is which account_id/kv_namespace_id/api_token
config.get_kv_config() resolves to. There is no separate local-only code path.
"""

import json
import time
from typing import Any
from urllib.parse import quote

import requests

KV_API_BASE = "https://api.cloudflare.com/client/v4"
# without one a hung connection hangs the whole cron tick indefinitely
TIMEOUT_SECONDS = 30
# KV allows one write per second per key - src/live_ticker.py and
# src/orchestration.py can both write the same week's games key in the
# same second, so a 429 gets one retry after this long
RATE_LIMIT_RETRY_SECONDS = 1.5


class KVError(RuntimeError):
    """Raised when a KV request fails (non-2xx response or success=False)."""


class KVClient:
    """Class for Cloudflare KV reads/writes - one JSON value per key."""

    def __init__(self, account_id: str, kv_namespace_id: str, api_token: str):
        self._values_url = (
            f"{KV_API_BASE}/accounts/{account_id}/storage/kv/namespaces/"
            f"{kv_namespace_id}/values"
        )
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {api_token}"})

    def _value_url(self, key: str) -> str:
        return f"{self._values_url}/{quote(key, safe='')}"

    def write(self, key: str, value: dict[str, Any]) -> None:
        """Write a single key's JSON value (replaces it entirely)."""
        body = json.dumps(value).encode("utf-8")
        response = self._put(key, body)
        if response.status_code == 429:
            time.sleep(RATE_LIMIT_RETRY_SECONDS)
            response = self._put(key, body)
        response.raise_for_status()
        data: dict[str, Any] = response.json()

        if not data.get("success"):
            raise KVError(data.get("errors"))

    def _put(self, key: str, body: bytes) -> requests.Response:
        return self._session.put(
            self._value_url(key),
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )

    def delete(self, key: str) -> None:
        """Delete a single key - a no-op if it doesn't exist."""
        response = self._session.delete(self._value_url(key), timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        data: dict[str, Any] = response.json()

        if not data.get("success"):
            raise KVError(data.get("errors"))

    def read(self, key: str) -> dict[str, Any] | None:
        """Read a single key's JSON value, or None if the key doesn't exist."""
        response = self._session.get(self._value_url(key), timeout=TIMEOUT_SECONDS)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
