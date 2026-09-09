"""Shared JSON REST clients (api-sports.io, The Odds API)."""

import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

logger: logging.Logger = logging.getLogger(__name__)

TIMEOUT_LIMIT = 120

# Status codes worth retrying on any plain JSON REST client here (429 is
# handled separately by ApiRateLimitError, not lumped in with these).
RETRYABLE_STATUS = {500, 502, 503, 504}


class ApiError(RuntimeError):
    """source covers which api had issue"""

    def __init__(self, source: str, message: str):
        self.source = source
        super().__init__(f"[{source}] {message}")


class ApiRateLimitError(ApiError):
    """429, or a rate/usage quota is exhausted"""


class ApiServerError(ApiError):
    """5xx from the API"""


class ApiDataError(ApiError):
    """Non-retryable: bad request, malformed payload, or an error the API
    itself reported despite a success-looking status."""


def fetch_and_validate(
    label: str, fetch: Callable[[], list[dict[str, Any]]], model: type[BaseModel]
) -> list[Any]:
    """call `fetch`, validate every item against `model`, log, return."""
    logger.info("Fetching %s", label)
    try:
        items = [model.model_validate(item) for item in fetch()]
        logger.info("Parsed %d %s", len(items), label)
        return items
    except Exception:
        logger.exception("Fetch failed for %s", label)
        raise


def fetch_and_validate_one(
    label: str, fetch: Callable[[], dict[str, Any]], model: type[BaseModel]
) -> Any:
    """call `fetch`, validate the single returned object against `model`, log, return."""
    logger.info("Fetching %s", label)
    try:
        item = model.model_validate(fetch())
        logger.info("Parsed %s", label)
        return item
    except Exception:
        logger.exception("Fetch failed for %s", label)
        raise
