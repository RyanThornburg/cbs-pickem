# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Identity

This is a python project to read and save data from CBS Pick Em Contest. The contest allows a user to pick five teams each week against the spread and keeps their record on how well they do.

## Commands

This project uses [uv](https://docs.astral.sh/uv/) for dependency management and running code.

- Run the app: `uv run main.py`
- Apply schema to a D1 database: `./setup.sh [local|prod]` (or `uv run python -m db.setup [local|prod]` directly)
- Scrape CBS and save weekly standings: `uv run python -m api.cbs_client`
- Fetch/validate api-sports.io data (teams/standings/games/etc., not persisted): `uv run python -m api.sports_io_client`
- Fetch/validate The Odds API data (spreads/totals odds, not persisted): `uv run python -m api.the_odds_api_client`
- Add a dependency: `uv add <package>`
- Add a dev dependency: `uv add --dev <package>`

Requires Python >=3.14 (pinned via `.python-version`).

Subsystem-specific conventions live in per-directory `CLAUDE.md` files,
loaded automatically whenever Claude Code reads/edits files in that
directory: `api/CLAUDE.md` (CBS scraping, Sports IO, The Odds API),
`config/CLAUDE.md`, `db/CLAUDE.md`. This root file only holds rules that
apply across the whole repo.

## Paths & Logging

All filesystem paths (`DATA_DIR`, `STATE_PATH`, `LOG_DIR`, the `.env` files)
are anchored to `config.config.PROJECT_ROOT` — or, for the `.env` files
specifically, `Path(__file__).parent` since they're colocated with
`config.py` itself — rather than the process's current working directory.
Don't introduce a bare relative `Path("something")` for anything that needs
to resolve consistently regardless of where a script gets invoked from.

Logging is centralized in `config.config.configure_logging()`. It passes
`force=True` to `logging.basicConfig()` so it always wins regardless of
import order or whatever else may have configured logging first, and it's
called explicitly by each entry point's own `if __name__ == "__main__":`
block — never at module import time, since importing a module shouldn't
have side effects like reconfiguring the root logger.
