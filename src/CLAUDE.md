# src/CLAUDE.md

## Loaders

`src/loaders/` is where scraped/fetched data actually gets written to D1 —
distinct from `api/`, which only fetches and validates. Each loader
follows the same shape: fetch via an `api/` client, resolve any external
(CBS/Sports IO) ids to internal D1 ids, build a list of
`(sql, params)` upsert statements, and run them through `D1Client.batch()`
in one atomic call.

**No function anywhere in this codebase takes an `env` parameter except
each module's own `if __name__ == "__main__":` block** (added 2026-09-15,
replacing the older pattern where every `load_*()`/`write_*()`/`main()`
each took `env: str = "local"` and independently called `load_env(env)`
itself). `env` never actually varies within a single process — one
invocation only ever targets local or prod, never both — so threading it
through every call was pure boilerplate with no real flexibility behind
it. `load_env(env)` is called **exactly once per process**, at the
bottom of whichever module was actually invoked from the command line
(`if not load_env(sys.argv[1] if len(sys.argv) > 1 else "local"): sys.exit(1)`
then `main()`) — after that, `os.environ` is populated for the rest of
the process, and every other function (`main()` included) takes no `env`
argument and never calls `load_env()` itself, trusting it already ran.
This is what lets `orchestration.py` call any loader's `load_*()`
function (or another module's `main()`, e.g. `teams_loader.main()`)
directly with no argument, and lets each module still work standalone
via its own `if __name__ == "__main__":` (`uv run python -m
src.loaders.X [local|prod]`) — only that one call site per module needs
`sys.argv`. `api/`'s own config getters (`get_cbs_config()`,
`get_sports_io_api()`, etc.) are unrelated to this and unchanged — they
already never took `env` (see `config/CLAUDE.md`).

- `season_loader.py` — upserts the current season (from Sports IO's
  `get_current_season()`) into `seasons`, clearing `is_active` on any
  other row first so at most one season is ever active.
- `teams_loader.py` — upserts all 32 teams (Sports IO's `/teams` +
  `/standings` merged, since conference/division only live on the
  latter) into `teams`, keyed on `sports_io_team_id`. Also carries
  `wins`/`losses`/`ties` (`Standing.won`/`lost`/`ties`, added 2026-09-15
  for a UI record display) - a plain overwrite of the team's current
  season record each run, not tracked historically per week (unlike
  odds, there's no "opening vs closing" concept here, just "what is it
  right now"). Originally this loader only ever ran manually
  (`src/new_season.py`'s bootstrap) since team profiles are static
  season-long data - the record fields are the first thing on this table
  that actually needs to stay fresh, so `orchestration.py`'s housekeeping
  now also calls `teams_loader.main(env)` daily (see Orchestration
  below) to keep them current; the rest of the row (name/city/logo/etc.)
  just gets harmlessly re-upserted with itself in the same call.
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
  `load_cbs_games()`/`load_cbs_user_picks()` both take an optional
  `pool_period_id` (added 2026-09-15, passed straight through to
  `get_cbs_pool_home()`/`get_cbs_weekly()` — see `api/CLAUDE.md`) — this
  is what `backfill_cbs_week(week_number)` uses to re-run both loaders
  against a specific already-elapsed week instead of always the current
  one, resolving the id from `weeks.cbs_pool_period_id` (already stored
  for every week). CLI: `uv run python -m src.loaders.cbs_loader
  [local|prod] <week_number>`.
- `sports_io_loader.py` — `load_games_data(live=False|True)` upserts
  `weeks` (start/end time, `live=False` only — a `live=True` call only
  ever sees currently-live games, so it can't safely compute a whole
  week's date range) and `games` (score/status always, plus a per-quarter
  score breakdown — `home_q1_score`..`home_q4_score`/`home_ot_score` and
  the `away_` equivalents, added 2026-09-10 for a scoreboard view. Sports
  IO's `Game.scores.{home,away}` already carried this
  (`QuarterScore.quarter_1`..`quarter_4`/`overtime`) but the loader
  wasn't persisting it. CBS has no equivalent field — this is a Sports
  IO-only column set, never written by `cbs_loader.py`; `stadium_id`/
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
- `pregame_weather_loader.py` (added 2026-09-15) — `load_pregame_weather()`
  captures a forecast for the *current* week's `SCHEDULED` games onto
  `games.forecast_*`, overwriting in place each run rather than keeping
  history (see `db/schema.sql`'s comment on those columns) — since only
  the current week's `SCHEDULED` games are ever re-queried, whatever's
  there when a game goes live is effectively "the forecast at kickoff",
  and `game_snapshots` already covers in-game/postgame conditions.
  Filters on `weeks.is_current`, not just `status = 'SCHEDULED'` alone -
  a real bug caught 2026-09-15 right after this shipped: housekeeping's
  `load_games_data()` seeds the *entire* season's schedule from Sports IO
  in one call (`league`/`season` params, not per-week), so every future
  week's games sit at `SCHEDULED` until actually played, not just the
  current one - without the `is_current` join this was firing ~250+
  Pirate Weather calls per run (the whole rest of the season) instead of
  the ~16 games in the current week alone. Confirmed live: dropped
  from 256 calls to 16 (10 captured + 6 skipped at enclosed stadiums)
  once fixed. Shares its actual fetch/dome-skip logic with
  `game_snapshots_loader.py` via `loader_helper.capture_weather()` (see
  below) rather than duplicating it — both loaders used near-identical
  weather-extraction code before this was pulled out.

`load_games_data(env, live=True)` fetches via
`_get_live_window_games()`, not `get_live_games()` — confirmed live
2026-09-11 that Sports IO's `live=all` filter (`get_live_games()`) stops
returning a game the instant it goes `FINAL`, so a poller that only ever
calls it can observe "still in progress" but can *never* observe the
transition to final; the game just silently vanishes from the response,
leaving `games.status` stuck at a stale `IN_PROGRESS` until the next
24h housekeeping full sync catches it (which also delayed
`_run_finished_game_stats()`'s catch-up, since that's gated on
`status = 'FINAL'`). `_get_live_window_games()` instead calls
`api.sports_io_client.get_games_by_date()` for both today's and
yesterday's UTC dates (yesterday too, so a game that kicked off just
before UTC midnight and is still within its live window isn't missed
because its own date field is "yesterday") — that endpoint returns every
game on the date regardless of status, so `FINAL` is visible the moment
Sports IO reports it. See `api/CLAUDE.md`'s Sports IO section for the
endpoint-level detail.

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

`loader_helper.capture_weather(latitude, longitude, roof_type, context)`
(added 2026-09-15) is the shared weather-fetch used by both
`game_snapshots_loader.py` and `pregame_weather_loader.py` — one Pirate
Weather call, folding in the enclosed-roof skip (`ENCLOSED_ROOF_TYPES`)
and the try/except-degrades-to-nulls behavior both call sites need, so
a fetch failure never blocks the caller's own write. Pulled out here
once a second loader needed the exact same logic `game_snapshots_loader.py`
already had — same "consolidate once two callers need it" reasoning as
`id_map()`/`sql_batch_call()`. Also captures `icon` (added 2026-09-15,
`game_snapshots.weather_icon`/`games.forecast_icon`) — Pirate Weather's
own standardized icon identifier (`DataPoint.icon`, e.g.
`"partly-cloudy-day"`, `"rain"`), distinct from `condition`'s free-text
summary and meant for the web UI to map onto an actual icon set rather
than parsing a summary string.

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

`orchestration.py`'s `run_tick()`/`main()` is **stateless per invocation** —
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
- **CBS picks, 2 min, the entire live window** — originally gated to
  "only before that week's Sunday deadline" (everything's visible via the
  deadline sweep after that, so a live poll seemed to have nothing left
  to do), but that guard was wrong: most games kick off *at or after* the
  deadline, and that's also when CBS reveals every entry's picks (not
  just early-game ones) — so the majority of live pick-grading
  (`is_correct`/`trending_status`/`trending_score`) was never actually
  being polled. Fixed 2026-09-10 by dropping the deadline check entirely;
  `_is_live_window_active()` alone now bounds the cost, same as every
  other live-only branch. Confirmed live 2026-09-09 that the deadline
  calc itself needed to be DST-aware
  (`_current_week_deadline_utc()`, using `zoneinfo`): a first version
  computed "the most recent Sunday" instead of "the upcoming Sunday" for
  Tuesday–Saturday, silently preventing the CBS branch from ever firing
  for the entire first half of a week. Pick'em weeks run Tue–Mon, so
  "this week's deadline" is the *upcoming* Sunday for Tue–Sat, today for
  Sunday itself, and yesterday for Monday. The deadline calc itself is
  still used by `_run_deadline_sweep()` below, just no longer gates this.
- **`game_snapshots`/live `game_team_stats`, 3 min, live only** — score
  moves every play, but weather/box-score stats don't need finer
  granularity than that, and `game_snapshots_loader.py` has its own
  additional dedup on top (skips a row entirely if the game clock hasn't
  moved since the last capture).
- **Odds, 6 hr baseline, quiet periods only** — a flat interval, plus a
  separate always-on pre-kickoff capture (see
  `_run_pre_kickoff_odds_capture()` below) for the game-day boost.
- **Pregame weather, 4 hr baseline / 1 hr once within 24h of kickoff,
  unconditionally every tick** (added 2026-09-15) — see
  `_run_pregame_weather_capture()` below. Two-tier like odds, but
  continuous rather than a single pre-kickoff pulse, since a forecast is
  worth re-checking repeatedly as it changes rather than just once right
  before kickoff.
- **Housekeeping, 24 hr, quiet periods only** — full Sports IO schedule
  refresh + `teams_loader.main()` (win/loss/tie records, added
  2026-09-15) + `load_cbs_weeks()`/`load_cbs_games()`. The CBS half of
  this exists specifically so `cbs_event_id`/`cbs_spread` are established
  for a new week *before* its first game goes live, since the CBS
  live-poll branch no longer does that itself (see below) — without it, a
  brand new week's first live game would have no way to resolve its
  picks.
- **CBS user picks, 30 min, quiet periods only** (added 2026-09-19) —
  `load_cbs_user_picks()` on its own cadence, independent of housekeeping's
  24h gate (`cbs_picks_quiet_last_poll_at`, `CBS_PICKS_QUIET_INTERVAL_SECONDS`).
  Added specifically to keep `weekly_performance.has_submitted_picks`
  fresh (see `kv_writer.py`'s leaderboard `has_submitted_picks` field
  below) - the CBS live branch already re-polls this every
  `CBS_LIVE_INTERVAL_SECONDS` while a game is live, but most users submit
  their picks well before that week's first kickoff, when the live branch
  never runs at all. Confirmed live 2026-09-19: a user's picks submitted
  Thursday night (right as that week's only live window was closing)
  stayed reported as "not submitted" until Sunday's deadline sweep -
  `has_submitted_picks` was the only thing not covered by any
  quiet-period polling, unlike `cbs_event_id`/`cbs_spread` which
  housekeeping already refreshes daily.

**CBS's live branch only calls `load_cbs_user_picks()`**, not
`load_cbs_games()` — a deliberate scope cut, confirmed live 2026-09-09
that Sports IO's score/status data is as good or better than CBS's for
the same fields, so re-fetching CBS's full pool-home page every 2 minutes
during every live window was pure waste. CBS is only load-bearing for two
things on `games` (`cbs_event_id`, the FK picks resolve through, and
`cbs_spread`, the actual line the pool grades against) and both now get
established once/day by housekeeping instead. It no longer calls
`write_current_week_leaderboard()` itself either (2026-09-15) — same
"unconditional at the bottom of `run_tick()` instead" move as
`write_current_week_games()` got 2026-09-11, see below.

The pre-kickoff odds capture (`_run_pre_kickoff_odds_capture()`), the
Sunday-deadline sweep (`_run_deadline_sweep()`), and the finished-games
stats catch-up (`_run_finished_game_stats()`) all run unconditionally on
every tick, live or quiet — the deadline sweep because it needs to fire
once regardless of whether a game happens to be live at that exact
moment, the stats catch-up because it's a stateless per-game flag check
(any `FINAL` game with `has_final_stats = FALSE`) rather than a
time-based gate, and the pre-kickoff odds capture because an already-live
early game (e.g. an early Sunday game running long) would otherwise
suppress it for an approaching later kickoff if it were nested inside the
quiet-only branch.

`_run_pre_kickoff_odds_capture()` (added 2026-09-11) is keyed off actual
`games.game_time` values, not hardcoded days/times - same reasoning as
`_is_live_window_active()`, since kickoff slots shift (byes,
international games, Thanksgiving). It fires once whenever any
not-yet-final game's kickoff is within `ODDS_PREKICKOFF_LEAD_MINUTES`
(30), gated by `ODDS_PREKICKOFF_MIN_GAP_SECONDS` (2 hr) rather than
per-slot state - that gap is what collapses a same-window doubleheader
(Sunday's 4:05/4:25 ET games) into one call instead of firing separately
for each distinct `game_time`, while still firing separately for TNF,
each distinct Sunday window, and MNF since those are hours apart. Adds
roughly 5 calls/week on top of the flat baseline - well within The Odds
API's 500/month allowance (see `CLAUDE.local.md`).

Both odds call sites (the flat baseline in `_run_quiet_period_tasks()`
and the pre-kickoff capture above) go through a shared `_capture_odds()`
helper, added 2026-09-11, that wraps `load_the_odds_api_odds()`/
`write_current_week_odds()` in a `try/except Exception:
logger.exception(...)` - odds are enrichment, not load-bearing, same
category as weather (see `game_snapshots_loader.py`'s ESPN/Pirate Weather
try/excepts in the Loaders section above), so a missing/invalid
`THE_ODDS_API_KEY` or an API outage must never block the deadline sweep,
finished-stats catch-up, or games KV write that run later in the same
tick. `logger.exception` is ERROR level, so it already lands in
`ERROR_LOG_FILE` (root `CLAUDE.md`'s "Paths & Logging") with no extra
plumbing - a real admin-page alert is still future work (see
`CLAUDE.local.md`'s TODO list), but the failure is at least captured
reviewably today, same "surface it somewhere, don't let it scroll by"
motivation as `mapping_gaps` (`db/CLAUDE.md`). The state key is updated
whether the capture succeeded or failed, specifically so a persistent
failure (e.g. a missing key) logs once per interval instead of retrying -
and failing - on every single minute-cron tick until it's fixed.

`_run_pregame_weather_capture()` (added 2026-09-15) queries for any
`SCHEDULED` game within `WEATHER_PREGAME_NEAR_WINDOW_HOURS` (24) of
kickoff to decide which of the two intervals gates it this tick - same
"query `games.game_time` directly rather than hardcode a day/time" shape
as `_run_pre_kickoff_odds_capture()`/`_is_live_window_active()`. Runs
unconditionally every tick (not nested in the live/quiet branch) for the
same reason the pre-kickoff odds capture does: a currently-live early
game shouldn't be able to suppress the forecast refresh for an
approaching later one. No separate KV write needed - `write_incomplete_weeks_games()`
already runs unconditionally at the end of `run_tick()` and picks up
whatever's newest in `games.forecast_*`.

`_run_finished_game_stats()` originally checked `NOT EXISTS (SELECT 1
FROM game_team_stats WHERE game_id = ...)` instead of a flag — confirmed
live 2026-09-10 that this never actually caught the live→FINAL
transition it was meant for: `load_live_game_statistics()` already
writes `game_team_stats` rows for a game every few minutes while it's
`IN_PROGRESS`, so by the time the game reaches `FINAL` a row already
exists and the `NOT EXISTS` check silently never fires — the stats left
in place are whatever the last live poll happened to capture, not a
confirmed final box score. Fixed by adding `games.has_final_stats`
(`BOOLEAN NOT NULL DEFAULT FALSE`, not a generated column since it
tracks something `game_team_stats` did, not a property of `games`
itself) — `_run_finished_game_stats()` selects FINAL games where it's
still `FALSE`, calls `load_game_statistics(week)` (which reloads every
game in that week, FINAL or not — safe/idempotent either way), then sets
the flag `TRUE` for that week's FINAL games so the same games aren't
reloaded on every subsequent tick.

`write_current_week_games(env)` (see "KV writer" below) originally ran
unconditionally at the end of `run_tick()`, live or quiet — same
category of fix as `has_final_stats` above, found the same way. A tick
that observes a game go live→FINAL correctly stops treating it as live
and takes the quiet branch, but `write_current_week_games()` used to
only run unconditionally *inside* the live branch; the games KV key
would then show a stale `IN_PROGRESS`/`live` block for up to 24h (the
next housekeeping run) after a game actually ended. Fixed 2026-09-11 by
moving the call out of `_run_live_updates()` to the bottom of
`run_tick()`, alongside `_run_deadline_sweep()`/`_run_finished_game_stats()` —
cheap regardless (a few small `SELECT`s + one KV write), so no reason to
gate it.

**Replaced with `write_incomplete_weeks_games(env)` 2026-09-15** — a
second, related staleness bug caught live the same day `weeks.is_complete`
was wired up: `write_current_week_games()` only ever refreshes
`weeks.is_current`'s own KV key, but `is_current` tracks CBS's own pool
period, which can flip to the next week before the outgoing week's actual
last game has finished (confirmed live - CBS's Monday-night game finished
*after* CBS's own pool period had already moved to the next week).
`games.status`/score themselves were never wrong (Sports IO's live poll
updates them off `game_time`, not `is_current`), but once `is_current`
moved on, that prior week's `games` KV key was frozen at its last
snapshot with no further write ever coming - a real user-visible bug (a
game showing `status: null`, a mid-game score, in KV forever). Swapped
for `write_incomplete_weeks_games()` (see "KV writer" below), which
refreshes every week with `is_complete = 0`, not just the CBS-current
one - it must run **before** `_run_finished_game_stats()` in `main()`
(not after), since that's what flips `is_complete` to `TRUE`; running
before means the exact tick a week's last game goes `FINAL` still reads
`is_complete = 0` for it and gets one final corrected write, rather than
being excluded from that tick's refresh by its own just-set completion
flag.

`write_current_week_leaderboard(env)` similarly moved to an unconditional
call at the bottom of `run_tick()` (2026-09-15, right after
`_run_finished_game_stats()`) — a different bug from the games one above,
but the same root shape: it used to only ever get called from inside the
CBS live branch (gated on an actual live game existing) or
`_run_deadline_sweep()`. Since `weeks.is_current` flips as soon as
housekeeping's daily `load_cbs_weeks()` run sees CBS's pool period
change - independent of whether any game is live yet - a new week could
sit as "current" for days (Tuesday through that week's first kickoff)
with **no `leaderboard` KV key written for it at all**, not just a stale
one. The old call sites weren't removed for redundancy's sake so much as
made unnecessary by this one; see the "KV writer" section below for
`write_current_week_leaderboard()` itself, which is unchanged - only
*when* it gets called changed here.

`write_admin_status(env)` (see "KV writer" below, `meta:admin`) runs
unconditionally at the end of `run_tick()`, after `write_season_trends()`
— cheap local reads, and an admin health check is most useful exactly
when something just failed, not stale.

## KV writer

`src/kv_writer.py` computes derived JSON blobs from D1 and writes them to
Cloudflare KV for `cbs-pickem-web`'s Worker to read — D1 stays the system
of record, KV is a serving cache (see root `CLAUDE.md`'s Commands list
and `CLAUDE.local.md`'s "Web UI" section for the overall architecture
decision). Eight keys total; six have a `write_*`/`write_current_week_*`
pair (the latter resolves `weeks.is_current` via `_resolve_current_week()`
then delegates):

- `write_meta_current()` → `meta:current` — `current_week` from
  `weeks.is_current`, plus `second_half_start_week` and `paid_places`
  (both config, see below) so the UI never hardcodes pool rules.
- `write_week_games()` → `week:{season}:{weekNN}:games` — schedule +
  picks (naturally empty pre-lock, `user_picks` only ever has
  locked/revealed rows) + a `live` block (down/distance/possession/
  weather from `game_snapshots`) present only while `games.status` is
  `IN_PROGRESS`/`HALFTIME` — a missing `live` key means no live data, not
  zeros. Also carries `stadium` (name/city/state/country/lat/lng/
  roof_type/surface_type, `None` if `games.stadium_id` isn't resolved
  yet) and `forecast` (added 2026-09-15, `games.forecast_*` — the
  pregame forecast captured by `src/loaders/pregame_weather_loader.py`,
  `None` until a capture has happened, permanently `None` for
  enclosed-roof stadiums) on every game, independent of live status -
  unlike the `live` block's weather, this is meant to be visible
  *before* kickoff (the actual point of it - helping a pick get made
  with the forecast in mind), and simply stops updating once a game goes
  live rather than disappearing. Each of `home_team`/`away_team` also
  carries a `record` (`{wins, losses, ties}`, added 2026-09-15 from
  `teams.wins`/`losses`/`ties` — see `src/loaders/teams_loader.py` above
  — `None` if that team hasn't synced a record yet). This is the team's
  *current* record as of the last daily housekeeping sync, not the
  record as it stood entering that specific game - no per-week history
  kept, same reasoning as the pregame forecast overwriting in place.
- `write_week_leaderboard()` → `week:{season}:{weekNN}:leaderboard` —
  cumulative/first-half/second-half scores and tie-aware `place`
  (`_standard_rank()`, standard competition ranking: ties share a place,
  the next place skips) computed here rather than by the web app.
  **No custom live-grading** — `is_correct`/`trending_status`/
  `trending_score` are CBS's own fields, passed through as-is; deriving
  provisional correctness from live scores was explicitly rejected during
  the original KV-contract design discussion (2026-09-10, not written down
  anywhere in-repo — the design doc was a Claude Artifact, not a file
  here). The actual
  computation lives in `compute_week_leaderboard(d1, week_number)`, split
  out from the write function specifically so `season_close_out.py` can
  reuse the identical math for a season's final standings — see below.
  Each user also gets `in_money_overall`/`in_money_first_half`/
  `in_money_second_half` booleans against the configured payout counts,
  plus `seasons_played` (added 2026-09-11, `_prior_seasons_by_user()`)
  computed from `historical_standings` rather than stored on `users` -
  deliberately not a persisted column since it's a pure derivation with no
  ongoing-maintenance win from storing it (see reasoning below). Every
  `historical_standings` row is a *closed* season, so a leaderboard
  entry's own count is that plus 1 for the current season itself (safe
  here specifically because every `user_id` on a week's leaderboard is by
  definition a confirmed current-season participant). No separate
  `is_rookie` flag - redundant with `seasons_played == 1`. Any future
  per-user key should reuse `_prior_seasons_by_user()` the same way,
  adding its own +1 only where the caller can make the same guarantee.
  Each user also carries `has_submitted_picks` (added 2026-09-19, plain
  `weekly_performance.has_submitted_picks` for *this* week) alongside
  `picks` — `picks` itself stays empty pre-lock by design (see
  `load_cbs_user_picks()` above, the whole point is not leaking picks
  early), which made it indistinguishable from "hasn't picked at all."
  Deliberately a sibling boolean rather than encoding the distinction as
  `null` vs `[]` on `picks` itself - `[]` is truthy in JS, so a consumer
  doing `if (picks)` would treat "submitted, still hidden" and "never
  submitted" as the same thing; an explicitly named field can't be
  misread that way. `picks` itself is unaffected and stays an array
  either way (never `null`).
- `write_week_odds()` → `week:{season}:{weekNN}:odds` — per game,
  `cbs_spread` (what the pool is graded against) alongside an
  opening/closing consensus spread. "Consensus" is the **mode**, not a
  mean — confirmed this is what was wanted (the value the most books
  agree on, e.g. "6 of 9 at -3", not a blended number that might not
  match any real line). Ties were originally broken by the median of the
  tied point values, but that's a real bug fixed 2026-09-19: a 2-way tie
  between adjacent half-point lines (e.g. -4.5 and -4) medians to -4.25 -
  not a number any book would ever actually offer, since real spreads
  only end in .0 or .5. `_consensus_line()` now breaks ties by juice
  instead - whichever tied point value has a book pricing it closest to
  standard -110 American odds is the one taken as the consensus, since
  books deliberately price away from -110 to compensate for offering a
  more/less generous number (bettor-friendlier costs more juice, stingier
  is cheaper) - the number still priced near -110 is the real market
  consensus, not a number nobody's actually offering at a discount or
  premium. Confirmed live on a real 2-way tie (Washington's closing line,
  2026-09-19): bovada's -4 at exactly -110 beat betrivers'/fanduel's -4.5
  at -107/-102, correctly resolving to -4 instead of the old -4.25.
  Restricted to `_ODDS_BOOKMAKERS` (draftkings/fanduel/betmgm/betrivers/
  bovada) — The Odds API also returns several offshore/enthusiast books
  (betus/lowvig/betonlineag/mybookieag) that update fast but aren't names
  worth citing in the UI. Each book's own earliest/latest
  `odds_snapshots` row stands in for "opening"/"closing" (matching the
  table's own MIN/MAX-over-`captured_at` design, see `db/CLAUDE.md`).
  Also carries `books` (added 2026-09-15, `_latest_book_odds_by_game()`) —
  each `_ODDS_BOOKMAKERS` book's own **latest** spread/total/moneyline
  line (`home_point`/`home_price`/`away_point`/`away_price`/
  `captured_at` per market), for a detailed per-book odds view. Unlike
  `market_spread`, this isn't a consensus and doesn't track open vs.
  close — a single book's own line only needs its most recent snapshot,
  same reasoning as `teams.wins`/`losses`/`ties` being "current, not
  historical." Confirmed live: over/under line-movement trends
  (`total_movers` in `write_week_trends()`, see below) already existed
  before this and needed no changes.
- `write_week_trends()` → `week:{season}:{weekNN}:trends` (added
  2026-09-13, `d05309d` — never actually folded into this doc until a
  2026-09-17 full-codebase review caught it; `CLAUDE.local.md`'s "Web UI"
  section had been saying this key was deferred/not built the whole time)
  — pick popularity + cold teams (zero picks after reveal) per game,
  one-sided games (≥ `_ONE_SIDED_THRESHOLD` consensus on one side, floored
  by `_ONE_SIDED_MIN_PICKS` so an early barely-revealed game can't
  qualify), "all alone" picks (exactly one user on a side against at
  least `_ALL_ALONE_MIN_OPPOSING` on the other), and this week's biggest
  spread/total line movers (open→close ≥ `_LINE_MOVER_MIN_POINTS`, reusing
  `_open_close_consensus_by_game()` from `write_week_odds()` above so the
  movers and the odds key's own open/close numbers can't disagree). No
  `write_current_week_trends()`-only wrapper distinction worth calling out
  beyond the usual pair — it's the same shape as `write_week_games()`/etc.
- `write_season_trends()` → `season:{season}:trends` (added 2026-09-13,
  same commit/doc-gap as above) — season-long pick totals per team, teams
  nobody in the pool has picked all season, each team's ATS cover record
  computed straight from `cbs_spread` + final scores via `_ats_side()`
  (independent of whether the pool ever actually picked that team, unlike
  the pick-popularity numbers), and every "all alone" pick logged all
  season. Shares `_split_home_away()`/`_all_alone_entries()`/
  `_game_team_dicts()` helpers with `write_week_trends()`. Has no
  `_current_week`-resolving wrapper since it isn't scoped to a week at
  all — called directly from `orchestration.py`.
- `write_historical()` → `meta:historical` — see `db/CLAUDE.md`'s
  `historical_standings` section for what feeds this.
- `write_admin_status()` → `meta:admin` (added 2026-09-11) — a health-check
  summary for an eventual admin page: when each `orchestration.py` task
  last ran (from `orchestration_state`) plus recent `mapping_gaps`/
  `system_events` rows to review. Staleness is only flagged for odds
  (combining its two cursors - the flat baseline and the pre-kickoff
  capture, since either one running recently means odds data is fresh)
  and housekeeping, using thresholds deliberately looser than
  `orchestration.py`'s own intervals (`_ODDS_STALE_SECONDS`/
  `_HOUSEKEEPING_STALE_SECONDS`, not imported from there to avoid a
  circular import - `orchestration.py` already imports from this module).
  The four live-only pollers (Sports IO/CBS live polls, `game_snapshots`,
  live `game_team_stats`) just report their raw last-run timestamp with
  no stale flag - "should this have run" for those depends on live-window
  history, which isn't worth the complexity for a first pass; most of the
  time they simply won't have run recently because nothing's live, and
  that's correct, not a problem. Unlike every other `write_*` function
  here, this one isn't called after a specific D1 write - it's called
  unconditionally at the end of `run_tick()`, same as
  `write_incomplete_weeks_games()`, since it's a handful of cheap local
  `SELECT`s and freshness matters most exactly when something just broke.
- `write_incomplete_weeks_games()` (added 2026-09-15, replacing
  `write_current_week_games()` as `orchestration.py`'s unconditional
  per-tick call) → refreshes `week:{season}:{weekNN}:games` for **every**
  week with `weeks.is_complete = 0`, not just `weeks.is_current`'s one.
  Fixes a real staleness bug: `is_current` tracks CBS's own pool period
  and can flip to the next week before the outgoing week's actual last
  game finishes (confirmed live), after which `write_current_week_games()`
  alone would never refresh that prior week's now-stale `games` KV key
  again - `game.status`/score were always correct in D1 (Sports IO's live
  poll doesn't care about `is_current`), the KV write was just the one
  thing that stopped happening for that week. `write_current_week_games()`
  itself still exists and is still used where "the current week
  specifically" is actually the right scope (`_run_deadline_sweep()`).

**Write-through, not polling or diffing**: every write function is
called immediately after the specific D1 write that could have changed
its underlying data (see `orchestration.py`'s call sites), not on its own
timer and not after checking whether anything actually changed. This was
a deliberate design choice over both alternatives — a timer decouples the
write from the actual change (stale between ticks or wasted no-op writes
when nothing changed), and diffing adds a read-before-write for no
correctness benefit since these writes are already cheap and idempotent.
`write_incomplete_weeks_games()` is the one exception (unconditional every
tick, not tied to one specific loader) precisely because *several*
different loaders can change what it shows, across however many weeks
aren't yet complete — see above.

`config.OVERALL_PAID_PLACES`/`FIRST_HALF_PAID_PLACES`/
`SECOND_HALF_PAID_PLACES` (added 2026-09-11, `_PAID_PLACES` in
`kv_writer.py`) are hand-set pool-admin rules, same convention as
`SECOND_HALF_START_WEEK` — currently 5/3/3, matching what the pool
actually pays out. Change these, not the leaderboard math, if the pool's
payout structure ever changes.

## Season close-out

`src/season_close_out.py` is the manual, once-a-year counterpart to
`src/new_season.py` — see root `CLAUDE.md`'s "End of season" section for
the full runbook. It always operates on `config.SEASON` (never a season
passed as an argument) because `compute_week_leaderboard()`, which it
reuses for the closing math, is itself hardcoded to `config.SEASON` —
accepting a different season id here would silently mislabel that
season's real data. Resolves the season's final week via
`MAX(week_number) FROM weeks`, computes that week's leaderboard, and
upserts one `historical_standings` row per user from
`entry["place"]`/`entry["cumulative_score"]`/`entry["first_half_place"]`/
`entry["first_half_score"]`/`entry["second_half_place"]`/
`entry["second_half_score"]`, then calls `write_historical()` to refresh
KV.

**Never run this before the season's real final week has been played** —
confirmed live 2026-09-10 that doing so produces a wrong result
silently: `compute_week_leaderboard()`'s only guard is "does any
`weekly_performance` row exist through this week," which is true the
moment week 1 has data, so calling it with, say, week 18 as the "final"
week while only week 1 has actually happened would compute cumulative
scores as if week 1 were the whole season and write that as the
season's official close-out.
