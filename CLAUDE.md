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
- Compute and write all KV keys the web UI reads (`meta:current`, that
  week's `games`/`leaderboard`/`odds`, `meta:historical`) from D1:
  `uv run python -m src.kv_writer [local|prod]` — normally called
  piecemeal from `src.orchestration`, not run whole like this except to
  force a full refresh
- **End-of-season close-out (manual, run once the season is truly
  over — see "End of season" below): `uv run python -m src.season_close_out [local|prod]`**
- One-off historical-standings backfill (already run once for 2013-2025 —
  see "End of season" below): `uv run python -m src.historical_backfill [local|prod]`
- Add a dependency: `uv add <package>`
- Add a dev dependency: `uv add --dev <package>`

Requires Python >=3.14 (pinned via `.python-version`).

Subsystem-specific conventions live in per-directory `CLAUDE.md` files,
loaded automatically whenever Claude Code reads/edits files in that
directory: `api/CLAUDE.md` (CBS scraping, Sports IO, The Odds API),
`config/CLAUDE.md`, `db/CLAUDE.md`, `src/CLAUDE.md` (loaders, orchestration).
This root file only holds rules that apply across the whole repo.

## End of season

Season boundaries are always a deliberate manual step in this project —
`config.SEASON` is hand-bumped once a year, `src/new_season.py` is a
manual per-season bootstrap. Closing out a finished season is the manual
teardown counterpart, and has to happen **before** `SEASON` gets bumped
for the next year (it always operates on whatever `config.SEASON`
currently is, never a season passed as an argument, since
`kv_writer.compute_week_leaderboard()` — which it reuses — is itself
hardcoded to `config.SEASON`).

Once the season's actual final week has been played (not before — running
this while games remain would silently treat a partial-season score as
final, since the "is there any data at all" check it relies on doesn't
distinguish partial from complete):

1. `uv run python -m src.season_close_out [local|prod]` — computes that
   season's final cumulative standings the same way the live weekly
   leaderboard always has, writes one `historical_standings` row per
   user, then refreshes `meta:historical` in KV.
2. Bump `config.SEASON` and run `src/new_season.py` for the new year, same
   as always.

`historical_standings`/`historical_user_mapping` were originally
backfilled once (2026-09-10) from a pre-2026 archive
(`data/{year}/{year}_standings.json`, 2013-2025) via
`src/historical_backfill.py` (which itself reuses
`src/historical_standings.py`'s per-year JSON parsing). Both of those
`historical_*` scripts are one-offs tied to that initial backfill — once
that's done and confirmed, they have no ongoing purpose (unlike
`season_close_out.py`, which runs every year going forward) and are
expected to be deleted from the repo eventually.

That archive isn't fully trustworthy as-is — confirmed live 2026-09-11
that the original `data/2025/2025_standings.json` was actually a
duplicate of 2024's data (fixed by hand; see `db/CLAUDE.md`). Season
2015/2016's saved standings are missing their actual champion entirely
and can't be recovered. First/second-half winners (`first_half_rank`/
`second_half_rank` etc. on `historical_standings`) are only filled in for
2025 (entered by hand) — see `CLAUDE.local.md`'s TODO list for the full
state of what's backfilled vs. not.

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
