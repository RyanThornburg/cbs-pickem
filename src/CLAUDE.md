# src/CLAUDE.md

## Loaders

`src/loaders/` is where scraped/fetched data actually gets written to D1 —
distinct from `api/`, which only fetches and validates. Each loader
follows the same shape: fetch via an `api/` client, resolve any external
(CBS/Sports IO) ids to internal D1 ids, build a list of
`(sql, params)` upsert statements, and run them through `D1Client.batch()`
in one atomic call. `main(env="local")` is each loader's CLI entry point
(`load_env(env)` then do the work); `if __name__ == "__main__":` calls
`configure_logging()` then `main(sys.argv[1] if ... else "local")`.

- `season_loader.py` — upserts the current season (from Sports IO's
  `get_current_season()`) into `seasons`, clearing `is_active` on any
  other row first so at most one season is ever active.
- `teams_loader.py` — upserts all 32 teams (Sports IO's `/teams` +
  `/standings` merged, since conference/division only live on the
  latter) into `teams`, keyed on `sports_io_team_id`.
- `cbs_loader.py` — CBS-side loading: `load_cbs_users()` (upsert
  `users` from CBS pool members), `map_cbs_to_sports_io()` (backfill
  `cbs_team_id` + CBS-only fields onto existing `teams` rows, matched by
  abbreviation), and `load_cbs_user_picks()` (upsert `weekly_performance`
  + `user_picks` from the weekly-standings page). `load_cbs_games()` is
  still a stub.

`cbs_loader._cbs_id_map(client, table, cbs_column, pk_column)` is the
pattern for resolving CBS's raw ids to our internal FKs before writing:
`SELECT pk_column, cbs_column FROM table WHERE cbs_column IS NOT NULL`,
turned into a `{cbs_id: internal_id}` dict once, then looked up per row
being written. `load_cbs_user_picks()` needs four of these (`users`,
`weeks`, `games`, `teams`) before it can safely build a single
`user_picks`/`weekly_performance` statement — CBS's own ids
(`cbsSlotId`, `cbsItemId`, `poolPeriodId`, member id) are never valid
values for `user_picks`'/`weekly_performance`'s FK columns directly, and
a lookup miss is logged and skipped rather than written with a wrong or
null FK. `load_cbs_games()` will need the same pattern (`weeks`/`teams`
lookups) once it's built.

`load_cbs_user_picks()` only persists a pick once its game is locked
(`game.is_locked`) — see `api/CLAUDE.md`'s CBS pick-data section for why
(the logged-in user's own picks leak early otherwise) and for which of
CBS's several pick-id fields (`cbsItemId` vs `itemId` vs `Pick.id`) is
safe to store as `user_picks.cbs_pick_id`.

## Orchestration

`src/new_season.py` is a one-off "start of season" bootstrap: season,
then teams, then CBS users, then the CBS↔Sports IO team mapper, in that
order (season first since `weeks`/`games` will eventually FK to it, even
though nothing does yet today). This is different in kind from
`src/orchestration.py` (currently just a TODO stub) — `new_season.py`
runs once per season on demand, `orchestration.py` is meant for the
recurring, trigger-based scheduling described in `CLAUDE.local.md`'s
"scheduler" section (game-day checks, the Sunday 1PM CBS update, Odds API
throttling). Don't conflate the two — a script that sequences other
loaders for a specific trigger belongs in `orchestration.py`, not
`loaders/`.
