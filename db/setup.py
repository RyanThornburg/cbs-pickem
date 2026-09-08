"""Apply db/schema.sql to a D1 database over the HTTP API.

Usage: uv run python -m db.setup [local|prod]
"""

import logging
import re
import sys

from config.config import SCHEMA_PATH, configure_logging, get_d1_config, load_env
from db.d1_client import D1Client, D1Error

logger = logging.getLogger(__name__)


_WORD_BEGIN = re.compile(r"\bBEGIN\b", re.IGNORECASE)
_WORD_END = re.compile(r"\bEND\b", re.IGNORECASE)


def _split_statements(sql: str) -> list[str]:
    """Split schema.sql into individual statements for D1's batch API.

    Splits on ';', except inside CREATE TRIGGER ... BEGIN ... END; bodies,
    whose internal ';' terminators aren't statement boundaries.
    """
    statements = []
    buffer: list[str] = []
    depth = 0
    for chunk in sql.split(";"):
        buffer.append(chunk)
        depth += len(_WORD_BEGIN.findall(chunk)) - len(_WORD_END.findall(chunk))
        if depth > 0:
            continue

        stripped = "\n".join(
            line
            for line in ";".join(buffer).splitlines()
            if not line.strip().startswith("--")
        ).strip()
        if stripped:
            statements.append(stripped)
        buffer = []
        depth = 0
    return statements


def main(env: str = "local") -> None:
    if not load_env(env):
        sys.exit(1)

    client = D1Client(**get_d1_config())
    statements = _split_statements(SCHEMA_PATH.read_text())

    logger.info("Applying %d statements to D1 (%s)", len(statements), env)
    try:
        client.batch([(stmt, None) for stmt in statements])
    except D1Error:
        logger.exception("Schema setup failed")
        sys.exit(1)

    logger.info("Database setup complete for %s environment!", env)


if __name__ == "__main__":
    configure_logging()
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
