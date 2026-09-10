# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Identity

This is a python project to read and save data from CBS Pick Em Contest. The contest allows a user to pick five teams each week against the spread and keeps their record on how well they do.

## Commands

This project uses [uv](https://docs.astral.sh/uv/) for dependency management and running code.

- Run the app: `uv run main.py`
- Apply schema to a D1 database: `./setup.sh [local|prod]` (or `uv run python -m db.setup [local|prod]` directly)
- Run the scheduler (meant for cron, `* * * * *`): `uv run python -m src.orchestration [local|prod]`
- One-off season bootstrap (season/teams/CBS users/team mapper): `uv run python -m src.new_season [local|prod]`
- Scrape CBS and save weekly standings: `uv run python -m api.cbs_client`
- Fetch/validate api-sports.io data (teams/standings/games/etc., not persisted): `uv run python -m api.sports_io_client`
- Fetch/validate The Odds API data (spreads/totals odds, not persisted): `uv run python -m api.the_odds_api_client`
- Fetch/validate a Pirate Weather forecast, not persisted: `uv run python -m api.weather_api`
- Fetch/validate ESPN's public scoreboard, not persisted: `uv run python -m api.espn_client`
- Load NFL stadiums (static seed): `uv run python -m src.loaders.stadiums_loader [local|prod]`
- Capture live game snapshots (score/quarter/weather/field position): `uv run python -m src.loaders.game_snapshots_loader [local|prod]`
- Load odds (The Odds API only, Sports IO odds not built): `uv run python -m src.loaders.odds_loader [local|prod]`
- Add a dependency: `uv add <package>`
- Add a dev dependency: `uv add --dev <package>`

Requires Python >=3.14 (pinned via `.python-version`).

Subsystem-specific conventions live in per-directory `CLAUDE.md` files,
loaded automatically whenever Claude Code reads/edits files in that
directory: `api/CLAUDE.md` (CBS scraping, Sports IO, The Odds API),
`config/CLAUDE.md`, `db/CLAUDE.md`, `src/CLAUDE.md` (loaders, orchestration).
This root file only holds rules that apply across the whole repo.

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

`configure_logging()` attaches three handlers: a console `StreamHandler`,
a `RotatingFileHandler` at `LOG_FILE` (`logs/log.log`) carrying every
level, and a second `RotatingFileHandler` at `ERROR_LOG_FILE`
(`logs/error.log`, added 2026-09-09) filtered to `ERROR`+ only via
`error_handler.setLevel(logging.ERROR)` — a quick "does anything need my
attention" check without scanning the full combined log. This is the
same "surface it somewhere reviewable, don't just let it scroll by"
motivation as `mapping_gaps` (see `db/CLAUDE.md`), but for real
exceptions/failures rather than unmapped external values — the two are
complementary, not overlapping: a mapping gap never raises, so it would
never show up in `error.log` on its own.
