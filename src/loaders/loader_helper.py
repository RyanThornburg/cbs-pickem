"""helper for src/loaders/"""

import logging
import sys
from datetime import UTC, datetime
from typing import Any

from config.config import get_d1_config
from db.d1_client import D1Client, D1Error

logger = logging.getLogger(__name__)


def sql_batch_call(
    statements: list[tuple[str, list[Any] | None]], client: D1Client | None = None
) -> None:
    """Run a batch of (sql, params) statements"""
    client = client or D1Client(**get_d1_config())
    try:
        client.batch(statements)
    except D1Error:
        logger.exception("Loading data failed")
        sys.exit(1)


def id_map(client: D1Client, table: str, column: str, pk_column: str) -> dict[Any, int]:
    """external value : internal id for each row in the table"""
    result = client.query(
        f"SELECT {pk_column}, {column} FROM {table} WHERE {column} IS NOT NULL"
    )
    return {row[column]: row[pk_column] for row in result.results}


_UPSERT_MAPPING_GAP_SQL = """
INSERT INTO mapping_gaps (
    source, entity_type, raw_value, context, first_seen_at, last_seen_at
)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(source, entity_type, raw_value) DO UPDATE SET
    last_seen_at = excluded.last_seen_at,
    occurrences = occurrences + 1
"""


def mapping_gap_statement(
    source: str, entity_type: str, raw_value: Any, context: str | None = None
) -> tuple[str, list[Any] | None]:
    """(sql, params) for one mapping_gaps upsert - append to whatever
    statements list a loader is already building right next to its
    logger.warning() on a lookup miss"""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        _UPSERT_MAPPING_GAP_SQL,
        [source, entity_type, str(raw_value), context, now, now],
    )
