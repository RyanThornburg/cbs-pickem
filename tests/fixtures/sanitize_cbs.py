"""Copy saved CBS payloads from data/ (gitignored) into tests/fixtures/cbs/
with pool members' names replaced by "Player N" and the logged-in account's
own blocks dropped, since the repo is public.

Usage: uv run python -m tests.fixtures.sanitize_cbs <week_number> [...]
"""

import json
import sys
from pathlib import Path
from typing import Any

from config.config import DATA_DIR

FIXTURE_DIR = Path(__file__).parent / "cbs"

# unmodeled, and they carry the logged-in account's own entry/name
_DROP_KEYS = ("myEntries", "myMembership", "poolSettings")
_NAMED_TYPES = {"Member", "FootballPickemEntry"}


def _anonymize(node: Any, names: dict[str, str]) -> Any:
    if isinstance(node, list):
        return [_anonymize(item, names) for item in node]
    if not isinstance(node, dict):
        return node
    node = {k: _anonymize(v, names) for k, v in node.items()}
    if node.get("__typename") in _NAMED_TYPES and "name" in node:
        # same real name -> same placeholder, across entry and member
        node["name"] = names.setdefault(node["name"], f"Player {len(names) + 1}")
    return node


def sanitize(source: Path) -> dict[str, Any]:
    data = json.loads(source.read_text(encoding="utf-8"))
    for key in _DROP_KEYS:
        data.pop(key, None)
    return _anonymize(data, {})


def main(week_numbers: list[int]) -> None:
    FIXTURE_DIR.mkdir(exist_ok=True)
    for week in week_numbers:
        for prefix in ("cbs_week", "cbs_pool_home"):
            name = f"{prefix}_{week:02d}.json"
            source = DATA_DIR / f"Week{week:02d}" / name
            (FIXTURE_DIR / name).write_text(
                json.dumps(sanitize(source), separators=(",", ":")), encoding="utf-8"
            )
            print(f"wrote {FIXTURE_DIR / name}")


if __name__ == "__main__":
    main([int(arg) for arg in sys.argv[1:]])
