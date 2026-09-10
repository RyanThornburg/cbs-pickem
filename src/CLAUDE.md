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
- `stadiums_loader.py` — upserts a static, hand-curated 38-row seed (30
  U.S. stadiums + 8 international venues actually on this season's
  schedule) into `stadiums`, keyed on `name`. Not fetched from any API —
  none exposes NFL venue data. See `api/CLAUDE.md`'s "no punting data"
  note for the same "checked, genuinely doesn't exist" pattern.
- `cbs_loader.py` — CBS-side loading: `load_cbs_users()` (upsert
  `users` from CBS pool members), `map_cbs_to_sports_io()` (backfill
  `cbs_team_id` + CBS-only fields onto existing `teams` rows, matched by
  abbreviation), `load_cbs_weeks()`/`load_cbs_games()` (upsert `weeks`/
  `games` from the pool-home page — `cbs_event_id`/`cbs_spread`/
  `tv_network` are the fields CBS is uniquely needed for, see
  `db/CLAUDE.md`'s reconciliation section for how this coexists with
  Sports IO also writing `games`), and `load_cbs_user_picks()` (upsert
  `weekly_performance` + `user_picks` from the weekly-standings page).
- `sports_io_loader.py` — `load_games_data(live=False|True)` upserts
  `weeks` (start/end time, `live=False` only — a `live=True` call only
  ever sees currently-live games, so it can't safely compute a whole
  week's date range) and `games` (score/status always; `stadium_id`/
  `is_international` resolved by matching Sports IO's `venue.name`
  against `stadiums.name`, with a small `VENUE_NAME_CORRECTIONS` map for
  the 2 confirmed cases where Sports IO's venue name is stale/generic —
  see `api/CLAUDE.md`'s ESPN section for the mirror-image correction
  table). `load_game_statistics(week)` (full week, meant for
  end-of-week/backfill) and `load_live_game_statistics()` (currently-live
  games only, meant for polling) both upsert `game_team_stats` through
  the same shared `_load_stats_for_game_ids()` helper — Sports IO's stats
  endpoint returns real partial stats mid-game, not just final box
  scores, confirmed live.
- `odds_loader.py` — `load_the_odds_api_odds()` upserts `odds_snapshots`
  from The Odds API (spreads/totals/moneyline, American odds format
  requested directly via `oddsFormat=american` rather than converting
  decimal odds). Matches events onto `games` via `odds_api_event_id` when
  already linked, falling back to `(home_team_id, away_team_id,
  game_time)` on first sighting and backfilling the id — exact-match
  works because `game_time` is normalized to the same UTC format
  everywhere (see `db/CLAUDE.md`). `load_sports_io_odds()` is a
  deliberate stub — sticking with The Odds API only for now, user's call.
- `game_snapshots_loader.py` — `load_game_snapshots()` captures one
  `game_snapshots` row per currently-live game: quarter/status/possession/
  score from CBS's pool-home page, down/distance/field position from
  ESPN's scoreboard (the only source that has it — see `api/CLAUDE.md`),
  weather from Pirate Weather at the stadium's lat/lng (skipped entirely
  for Dome/Retractable roofs). Skips writing a new row if
  `(quarter, time_remaining)` is identical to the last capture for that
  game — the clock hasn't moved, so nothing happened. A CBS or ESPN fetch
  failure degrades gracefully (that source's fields come back null,
  everything else still gets captured) rather than aborting the whole
  snapshot.

`src/loaders/loader_helper.py` is the write-side counterpart to
`api/api_helper.py`'s shared read-side plumbing — every loader in this
directory uses it rather than defining its own copy (consolidated
2026-09-09; each loader used to have its own near-identical
`_sql_batch_call()`/id-map helper). `id_map(client, table, column,
pk_column)` is the pattern for resolving a source's raw external id to
our internal FK before writing: `SELECT pk_column, column FROM table
WHERE column IS NOT NULL`, turned into a `{external_value: internal_id}`
dict once, then looked up per row being written. `load_cbs_user_picks()`
needs four of these (`users`, `weeks`, `games`, `teams`) before it can
safely build a single `user_picks`/`weekly_performance` statement —
CBS's own ids (`cbsSlotId`, `cbsItemId`, `poolPeriodId`, member id) are
never valid values for `user_picks`'/`weekly_performance`'s FK columns
directly, and a lookup miss is logged and skipped rather than written
with a wrong or null FK. `load_cbs_games()`/`sports_io_loader.py`'s
`load_games_data()` use the same pattern for their `weeks`/`teams`
lookups. `sql_batch_call(statements, client=None)` runs a batch
atomically, building its own `D1Client` if the caller doesn't already
have one open for its own queries.

`loader_helper.mapping_gap_statement(source, entity_type, raw_value,
context)` is the other half — every genuine `id_map()`/correction-table
lookup miss (not an expected/transient one, see `db/CLAUDE.md`'s
`mapping_gaps` section) appends its `(sql, params)` result to a
loader-local `gap_statements` list, kept separate from the loader's real
`statements` list and only combined at the final `sql_batch_call(...)`
call — mixing them into one list was a real bug caught during this
session's testing (it silently inflated "Upserted N rows" log counts).
`db/CLAUDE.md` has the full rationale and the `mapping_gaps` table shape.

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
`src/orchestration.py` — `new_season.py` runs once per season on demand,
`orchestration.py` runs continuously (meant to be invoked by cron every
minute, `* * * * *`). Don't conflate the two — a script that sequences
other loaders for a specific recurring trigger belongs in
`orchestration.py`, not `loaders/`.

`orchestration.py`'s `run_tick(env)` is **stateless per invocation** —
every tick does two cheap local D1 checks (`_is_live_window_active()`,
the deadline/finished-games checks) and only calls out to an external API
when one of several interval gates says enough time has passed. The gates
live in a small `orchestration_state` key-value table (see
`db/schema.sql`), each key holding an ISO8601 UTC timestamp of when that
thing last ran — this is what lets a stateless, repeatedly-invoked
process behave like a real scheduler without needing its own persistent
process or internal sleep loop.

`_is_live_window_active()` decides live-vs-quiet branch from `games.game_time`
alone (`game_time <= now <= game_time + 4h AND status NOT IN ('FINAL',
'CANCELLED', 'POSTPONED')`) — deliberately not from `games.status`, since
status might just be stale (that's exactly what the live poll exists to
fix). This is a pure local query, no external call, so checking it every
minute costs nothing even during a multi-month off-season.

Cadences, and why each one is what it is:

- **Sports IO, 1 min, live only** — score/status. The fastest thing
  polled, and the only thing that needs to be.
- **CBS picks, 2 min, live and only before that week's Sunday deadline** —
  after the deadline everything's already visible via the deadline sweep,
  so a live CBS poll has nothing left to do. Confirmed live 2026-09-09
  that this needed a real DST-aware date calculation
  (`_current_week_deadline_utc()`, using `zoneinfo`): a first version
  computed "the most recent Sunday" instead of "the upcoming Sunday" for
  Tuesday–Saturday, silently preventing the CBS branch from ever firing
  for the entire first half of a week. Pick'em weeks run Tue–Mon, so
  "this week's deadline" is the *upcoming* Sunday for Tue–Sat, today for
  Sunday itself, and yesterday for Monday.
- **`game_snapshots`/live `game_team_stats`, 10 min, live only** — score
  moves every play, but weather/box-score stats don't need finer
  granularity than that, and `game_snapshots_loader.py` has its own
  additional dedup on top (skips a row entirely if the game clock hasn't
  moved since the last capture).
- **Odds, 6 hr baseline, quiet periods only** — a flat interval, no
  game-day boost yet (see the TODO in `CLAUDE.local.md`).
- **Housekeeping, 24 hr, quiet periods only** — full Sports IO schedule
  refresh + `load_cbs_weeks()`/`load_cbs_games()`. The CBS half of this
  exists specifically so `cbs_event_id`/`cbs_spread` are established for
  a new week *before* its first game goes live, since the CBS live-poll
  branch no longer does that itself (see below) — without it, a brand
  new week's first live game would have no way to resolve its picks.

**CBS's live branch only calls `load_cbs_user_picks()`**, not
`load_cbs_games()` — a deliberate scope cut, confirmed live 2026-09-09
that Sports IO's score/status data is as good or better than CBS's for
the same fields, so re-fetching CBS's full pool-home page every 2 minutes
during every live window was pure waste. CBS is only load-bearing for two
things on `games` (`cbs_event_id`, the FK picks resolve through, and
`cbs_spread`, the actual line the pool grades against) and both now get
established once/day by housekeeping instead.

The Sunday-deadline sweep (`_run_deadline_sweep()`) and the
finished-games stats catch-up (`_run_finished_game_stats()`) both run
unconditionally on every tick, live or quiet — the deadline sweep because
it needs to fire once regardless of whether a game happens to be live at
that exact moment, and the stats catch-up because it's a stateless
`NOT EXISTS` check (any `FINAL` game with no `game_team_stats` row yet)
rather than a time-based gate, so it costs nothing to just always check.
