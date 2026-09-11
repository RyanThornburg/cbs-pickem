"""Client for Cloudflare KV's HTTP storage API.

Used identically for every environment (local dev and production) - the only
difference between them is which account_id/kv_namespace_id/api_token
config.get_kv_config() resolves to. There is no separate local-only code path.
"""

import json
from typing import Any
from urllib.parse import quote

import requests

KV_API_BASE = "https://api.cloudflare.com/client/v4"


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
        response = self._session.put(
            self._value_url(key),
            data=json.dumps(value).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()

        if not data.get("success"):
            raise KVError(data.get("errors"))

    def read(self, key: str) -> dict[str, Any] | None:
        """Read a single key's JSON value, or None if the key doesn't exist."""
        response = self._session.get(self._value_url(key))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
