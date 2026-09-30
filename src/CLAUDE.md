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
(`run_cli(main)`, see `config/CLAUDE.md`) — after that, `os.environ` is populated for the rest of
the process, and every other function (`main()` included) takes no `env`
argument and never calls `load_env()` itself, trusting it already ran.
This is what lets `orchestration.py` call any loader's `load_*()`
function (or another module's `main()`, e.g. `teams_loader.load_teams()`)
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
  now also calls `teams_loader.load_teams()` daily (see Orchestration
  below) to keep them current; the rest of the row (name/city/logo/etc.)
  just gets harmlessly re-upserted with itself in the same call.
- `stadiums_loader.py` — upserts a static, hand-curated 38-row seed (30
  U.S. stadiums + 8 international venues actually on this season's
  schedule) into `stadiums`, keyed on `name`. Not fetched from any API —
  none exposes NFL venue data (confirmed live that api-sports.io has no
  `/venues` endpoint for American football).
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
  scores, confirmed live. Only regular-season games are loaded
  (2026-09-27): anything whose `game.week` isn't `Week N`
  (`_regular_season_week_number()`) is dropped along with preseason,
  since this pool is regular season only. Playoff games were never
  mappable anyway (named weeks like "Wild Card" with no `weeks` row,
  `team.id: 0` placeholders for undetermined matchups) and used to log
  the same five known-noise `mapping_gaps` rows on every daily run.
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
  `game_snapshots` row per game ESPN reports as in progress (status
  `state == "in"`, which covers halftime and delays), all game state from
  ESPN's scoreboard - quarter/clock/status/score/possession plus down/
  distance/field position/last play/win probability - and weather from
  Pirate Weather at the stadium's lat/lng (skipped entirely for Dome/
  Retractable roofs). **ESPN-only since 2026-09-27**: quarter/clock/score/
  possession used to come from CBS's pool-home page, but measured live
  that day, Sports IO's clock sat still for 2+ minutes while ESPN's ran
  and CBS's clock lags ESPN too - and dropping CBS also removed a
  Playwright scrape from every capture. Candidate games are any not-FINAL
  game that kicked off in the last `game_rules.LIVE_WINDOW_HOURS` (6), so a
  kickoff is picked up as soon as ESPN shows it, not when Sports IO's
  `games.status` catches up. Skips writing a new row if quarter/clock/
  score **and** ESPN's `last_play_id` are all identical to the last
  capture for that game - nothing happened. Returns the game_ids that got
  a new row, so `src/live_ticker.py` only rewrites KV when something
  moved. Possession is `None` when ESPN leaves it out (e.g. right after a
  kickoff return). Weather is refreshed at most every
  `WEATHER_REFRESH_SECONDS` (5 min) per game; rows in between carry the
  previous reading forward, `weather_captured_at` saying when it was
  actually fetched - keeps the 15s cadence from multiplying Pirate
  Weather calls (20,000/month plan). An ESPN fetch failure means no
  snapshots that round (logged, not raised); a Pirate Weather failure just
  leaves the weather columns null. `has_candidate_games()` is the one-query
  check the ticker uses to exit straight away outside game time.
- `player_stats_loader.py` (added 2026-09-27) — per-player box scores
  from Sports IO's `/games/statistics/players` into `game_player_stats`,
  one row per game/team/stat group/player with the stats as JSON. Groups:
  Passing, Rushing, Receiving, Fumbles, Interceptions, Defensive,
  Kick_returns, Punt_returns, Kicking, Punting (a group is just absent
  when nobody on that team has a line in it). Stat names are snake_cased
  (`"passing touch downs"` → `passing_touch_downs`), numeric strings
  become numbers, compound ones stay strings (`comp_att: "19/34"`,
  `sacks: "2-19"`), null stays null (common - confirmed live over a
  41-game 2025 sample). Live values match ESPN's exactly and update
  mid-game (confirmed live 2026-09-27). Polled every minute, so it diffs
  against the stored rows and only upserts rows whose stats changed and
  deletes lines Sports IO dropped - a few rows per game per minute rather
  than ~90. One `sql_batch_call` per game, since a week at once is ~1,300
  statements. `load_live_player_stats()`/`load_week_player_stats(week)`
  both return the changed game_ids for the KV write. Weeks 1-3 of 2026
  loaded on prod 2026-09-27.
- `win_probability_loader.py` (added 2026-09-27) — ESPN's full per-play
  win probability curve from its summary endpoint
  (`api.espn_client.get_summary()`), one `game_win_probability` row per
  FINAL game (JSON, written once). Each ESPN point is keyed to a drive
  play by `playId` for its period/clock/score; the one point that matches
  no play is ESPN's pre-kickoff value (confirmed live across all 43 FINAL
  2026 games: 159-226 points each, exactly one unmatched). Only fetches
  FINAL games without a row, within `RETRY_WINDOW_DAYS` of kickoff - so
  it's one D1 query when there's nothing to do, and a game ESPN never has
  a curve for stops being retried. The live value was already on every
  `game_snapshots` row; this is the gap-free version. Week CLI arg
  replaces a week's curves; weeks 1-3 of 2026 loaded on prod 2026-09-27.
- `scoring_plays_loader.py` (added 2026-09-27) — `load_scoring_plays()`
  replaces a game's `game_scoring_plays` rows from Sports IO's
  `/games/events`, but only for live (or FINAL within
  `FINAL_RECHECK_HOURS` of kickoff) games whose latest stored play isn't
  at the current `games.home_score`/`away_score` - so with nothing new
  it's one D1 query and no API calls, cheap enough for every tick. That
  score check is sound because every scoring play raises the combined
  score and Sports IO returns them in order (confirmed live across all
  272 2025 regular-season games, 0 exceptions). Delete-and-replace rather
  than upsert since events have no id, and it picks up Sports IO's own
  corrections. An empty event list never wipes stored rows (Sports IO's
  events can lag the score by a tick). `minute` is null on ~15% of plays
  (2025: 355 of 2,339) - stored as a null `clock`, order comes from
  `sequence`. Quarter names map `First`..`Fourth`/`Overtime` → 1-5, an
  unknown one is a `mapping_gaps` row. Types seen: `TD`, `FG`, `SF` and
  `Safety` (both), `2PTC`, and one truncated `Pass Interception Re` -
  stored raw. `backfill_week_scoring_plays(week)` (CLI: a week number
  after the env) skips the score check, for weeks that predate this -
  weeks 1-3 of 2026 backfilled on prod 2026-09-27 (46 games, every FINAL
  game's last play matched its final score).
- `pregame_weather_loader.py` (added 2026-09-15) — `load_pregame_weather()`
  captures a forecast for the *current* week's `SCHEDULED` games onto
  `games.forecast_*`, overwriting in place each run rather than keeping
  history (see `db/schema.sql`'s comment on those columns) — since only
  the current week's `SCHEDULED` games are ever re-queried, whatever's
  there when a game goes live is the last forecast captured before
  kickoff, and `game_snapshots` already covers in-game/postgame conditions.
  Values come from `loader_helper.capture_pregame_forecast()`: Pirate
  Weather's `hourly` entry for the kickoff hour (not `currently`), plus
  `games.forecast_window_*`, a summary (max precip chance and the precip
  type at that hour, max gust, temp low/high, summed snow accumulation)
  over the first `FORECAST_WINDOW_HOURS` (3) after kickoff. That's
  deliberately shorter than `GAME_DURATION_HOURS` (4, alert filtering
  only), since weather in the last hour barely matters. A kickoff off the
  hour (e.g. 8:15) touches one extra hourly entry. A kickoff past the hourly horizon (168h) but within
  daily's (8 days) falls back to that day's `daily` entry
  (`games.forecast_source = 'daily'`, whole-day values: no kickoff
  temp/feels-like, window temps are the day's min/max) and is replaced
  by hourly on the first capture once in range; past both, nothing is
  stored. Never falls back to `currently`. Fixed 2026-09-27: until then
  every `forecast_*` value was actually `currently`, i.e. conditions
  whenever the capture happened to run (Tuesday's weather for a Sunday
  game) - only the alert filter was ever kickoff-scoped.
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
- `espn_loader.py` (added 2026-09-26) - `load_espn_games()` sets
  `games.neutral_site` (ESPN's `competitions[].neutralSite`) and links
  `games.espn_event_id` up front, one ESPN scoreboard call per week
  (`get_scoreboard(week)`). Runs from daily housekeeping (after
  `load_games_data()`, so the rows exist) scoped to `is_complete = 0`
  weeks, since neither value changes once a game is played; the CLI form
  covers every week for a first run/backfill. Matches by
  `(home_abbrev, away_abbrev)` within a week, same as
  `game_snapshots_loader.py`. A failed week fetch is logged and skipped.
  Confirmed live 2026-09-26: all 272 games linked, the 9 `neutralSite`
  games are exactly the 9 `is_international` games, and ESPN's home/away
  designation matches Sports IO's for all of them.

`load_games_data(live=True)` fetches via
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
have one open for its own queries. A `D1Error` is logged and re-raised
(2026-09-27) - it used to `sys.exit(1)`, and since `SystemExit` isn't an
`Exception`, that slipped past `orchestration.py`'s soft-fail try/excepts
(odds, Sports IO live poll, pregame weather, scoring plays) and ended the
whole tick. Standalone runs still exit non-zero, just with a traceback.

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

Weather alerts (2026-09-27) are filtered in `_overlapping_alerts()` two
ways before they're stored: by time (overlapping the capture instant for
live snapshots, or kickoff through `GAME_DURATION_HOURS` for pregame) and
by type (`is_game_relevant_alert()` - a title-prefix denylist,
`IRRELEVANT_ALERT_PREFIXES`, of coastal/marine types like Rip Current,
Beach Hazards and Coastal Flood, all seen live on prod before this
existed). A denylist rather than an allowlist so an unseen alert type is
still shown. Stored as a JSON list (`games.forecast_alerts_json`,
`game_snapshots.weather_alerts_json`: `[{title, severity, starts,
expires, uri}]`) and exposed as `weather_alerts` (a list, `[]` if none)
in both the games KV key's `forecast` and `live.weather` blocks -
replacing an older single `"; "`-joined title string, whose columns were
converted (title only, other fields null) and dropped.

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

`orchestration.py`'s `main()` is **stateless per invocation** —
every tick does two cheap local D1 checks (`_is_live_window_active()`,
the deadline/finished-games checks) and only calls out to an external API
when one of several interval gates says enough time has passed. The gates
live in a small `orchestration_state` key-value table (see
`db/schema.sql`), each key holding an ISO8601 UTC timestamp of when that
thing last ran — this is what lets a stateless, repeatedly-invoked
process behave like a real scheduler without needing its own persistent
process or internal sleep loop.

The helpers both cron processes share - `soft()`, `run_and_record()`,
`run_on_interval()`,
the `orchestration_state` cursor functions (`get_state()`/`set_state()`/
`should_run()`), `record_system_event()` and `acquire_lock()` - live in
`src/scheduling.py` (moved out of `orchestration.py` 2026-09-27 when
`src/live_ticker.py` needed them too).

**No task can end the tick** (2026-09-27). Every task, including the KV
writes at the end of `main()`, runs through `soft()`: an exception is
logged (`logger.exception`, so it lands in `error.log`) and recorded as a
`system_events` row, and the tick moves on. Before this, only odds,
pregame weather, the Sports IO live poll and the newer enrichment steps
were wrapped - a failing CBS scrape (live or quiet poll, housekeeping, the
deadline sweep) ended the tick before snapshots, stats and every KV write,
including `meta:admin`, and since its cursor was only set on success it
retried the CBS login every minute. Interval tasks go through
`run_on_interval(client, source, task, cursor_key, success_key,
interval)`: the cursor moves on every attempt, so a task that keeps
failing retries once per interval rather than every tick, and
`success_key` moves only when it worked (what `meta:admin` reads). That
soft-run-then-stamp step is `run_and_record(client, source, task,
cursor_key, success_key)` on its own, used directly by the tasks that
decide when to run some other way (win probability and scoring plays
every tick, odds, pregame weather's two cadences, live_ticker's
snapshots).
Housekeeping runs its steps independently (one failing doesn't skip the
rest) but still counts as failed if any step did. The two tasks gated on
something other than an interval - the deadline sweep (a date) and the
finished-game stats catch-up (a flag) - wait `FAILURE_RETRY_SECONDS` (10
min) after a failure before trying again, via
`deadline_sweep_last_attempt_at`/`finished_game_stats_last_failure_at`.
A week whose final team stats fail keeps `has_final_stats` unset so it's
retried; its final player stats stay soft-fail as before.

**One tick at a time** (2026-09-27): the `__main__` block takes a
non-blocking `fcntl` lock (`scheduling.acquire_lock()`, `config.LOCK_DIR`,
`locks/orchestration.{env}.lock`, gitignored) and exits if the previous
tick is still running - cron starts one every minute regardless, and two
overlapping ticks would both pass the same `should_run()` checks and
double every call. `live_ticker.py` takes its own
(`locks/live_ticker.{env}.lock`), so the two never block each other. Per env, so a local tick
never blocks a prod one. Only works while every tick for an env runs on one
machine. `D1Client`/`KVClient` also got request timeouts the same day (60s/
30s - the API clients already had `TIMEOUT_LIMIT`), since a hung Cloudflare
connection used to hang a tick indefinitely.

`_is_live_window_active()` decides live-vs-quiet branch from `games.game_time`
alone (`game_time <= now <= game_time + LIVE_WINDOW_HOURS AND status NOT IN
DONE_STATUSES`) — deliberately not from `games.status`, since
status might just be stale (that's exactly what the live poll exists to
fix). This is a pure local query, no external call, so checking it every
minute costs nothing even during a multi-month off-season.
`LIVE_WINDOW_HOURS` is 6 (4 until 2026-09-28, then 5; 6 since 2026-09-29,
matching the snapshot loader's candidate window, which had been a separate
6): week 3's SNF ran about 3h40m and Sports IO took another ~6 minutes to
mark it FINAL, and 6 leaves room for overtime plus a long weather delay.
It lives in `src/game_rules.py` with `LIVE_STATUSES` (`IN_PROGRESS`/
`HALFTIME`/`DELAYED`) and `DONE_STATUSES` (`FINAL`/`CANCELLED`/
`POSTPONED`), which every live query uses - `sql_list()` renders one for
an `IN` clause. DELAYED counts as live everywhere since 2026-09-29; before
that the live team/player stats polls skipped it, so stats went stale
through a weather delay. A game still not FINAL past the window
stops being polled and sits `IN_PROGRESS` until the next daily
housekeeping run fixes it - deliberately not polled indefinitely. The
longer window costs nothing on a normal day, since a game leaves it as
soon as it's FINAL.

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
- **`game_snapshots`, 15 s, during games - not in this module** (3 min
  until 2026-09-27, then 1 min, then moved to `src/live_ticker.py` the
  same day) - see "Live ticker" below.
- **Live `game_team_stats`, 3 min, live only** - its own
  `LIVE_GAME_STATS_INTERVAL_SECONDS`, separate from snapshots.
- **Live player stats, 1 min, live only** (added 2026-09-27) -
  `LIVE_PLAYER_STATS_INTERVAL_SECONDS`, one Sports IO call per live game,
  roughly 2,000 on a full Sunday against the 7,500/day quota. Rewrites
  the changed games' `game:{season}:{game_id}:details` KV keys right
  after (the live team stats step does the same for its games).
  try/except with its own success cursor (`live_player_stats_capture` in
  `meta:admin`). The final capture rides along in
  `_run_finished_game_stats()` next to the final team stats, also
  soft-fail so `has_final_stats` still gets set.
- **Win probability curve, every tick, live window or not** (added
  2026-09-27) - `_run_win_probability_capture()`, see
  `win_probability_loader.py` above. Runs after `_run_finished_game_stats()`.
  try/except with its own success cursor (`win_probability_capture` in
  `meta:admin`).
- **Scoring plays, every tick, live window or not** -
  `_run_scoring_plays_refresh()`, see `scoring_plays_loader.py` above for
  why that's cheap. Runs before `write_incomplete_weeks_games()` so a new
  score's play reaches KV the same tick. try/except with its own success
  cursor, surfaced in `meta:admin` as `scoring_plays_refresh`.
- **`should_run()` has 30s of slack** (`_SHOULD_RUN_SLACK_SECONDS`, added
  2026-09-27). Each cursor is stamped when its task finishes, so on a
  60s cron the next tick always lands a few seconds short - without the
  slack every interval silently rounded up a whole tick (confirmed live:
  "3 min" snapshots were 4 min apart, so the "1 min" Sports IO live poll
  was really every 2 min). Harmless for long intervals, and can't
  double-run anything since cron only ticks once a minute.
- **Odds, 6 hr baseline, quiet periods only** — a flat interval, plus a
  separate always-on pre-kickoff capture (see
  `_run_pre_kickoff_odds_capture()` below) for the game-day boost.
- **Pregame weather, 4 hr baseline / 1 hr once within 24h of kickoff,
  unconditionally every tick** (added 2026-09-15) — see
  `_run_pregame_weather_capture()` below. Two-tier like odds, but
  continuous rather than a single pre-kickoff pulse, since a forecast is
  worth re-checking repeatedly as it changes rather than just once right
  before kickoff.
- **Recap KV keys, 5 min, live or quiet** (added 2026-09-28) -
  `_run_recap_refresh()`: the current week plus any week in progress
  or finished in the last 12 hours, see "KV writer" below.
- **Housekeeping, 24 hr, quiet periods only** — full Sports IO schedule
  refresh + `teams_loader.load_teams()` (win/loss/tie records, added
  2026-09-15) + `load_cbs_weeks()`/`load_cbs_games()` + `load_espn_games()`
  + `write_meta_current()` + the current week's odds key
  (`write_current_week_odds()`, added 2026-09-29) + the future weeks' games KV keys
  (`write_incomplete_weeks_games(include_future=True)`, see "KV writer").
  The odds step is there because `load_cbs_weeks()` is what moves
  `is_current` to a new week, and the odds key is otherwise only written
  right after an odds capture, which runs earlier in the same tick - found
  2026-09-29 when week 4 flipped at 01:46 UTC straight after that tick's
  capture had rewritten week 3's key, leaving `week:2026:04:odds` missing
  until the next 6-hour capture. The CBS half of
  this exists specifically so `cbs_event_id`/`cbs_spread` are established
  for a new week *before* its first game goes live, since the CBS
  live-poll branch no longer does that itself (see below) — without it, a
  brand new week's first live game would have no way to resolve its
  picks.
- **CBS user picks, 30 min, quiet periods only** (added 2026-09-19) —
  `load_cbs_user_picks()` on its own cadence, independent of housekeeping's
  24h gate (`cbs_picks_quiet_last_poll_at`, `CBS_PICKS_QUIET_INTERVAL_SECONDS`).
  Added specifically to keep `weekly_performance.has_submitted_picks`
  fresh (see `kv_writer/leaderboard.py`'s `has_submitted_picks` field
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
"unconditional at the bottom of `main()` instead" move as
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
`write_current_week_odds()` in `soft()` - odds are enrichment, not load-bearing, same
category as weather (see `game_snapshots_loader.py`'s ESPN/Pirate Weather
try/excepts in the Loaders section above), so a missing/invalid
`THE_ODDS_API_KEY` or an API outage must never block the deadline sweep,
finished-stats catch-up, or games KV write that run later in the same
tick. `logger.exception` is ERROR level, so it already lands in
`ERROR_LOG_FILE` (root `CLAUDE.md`'s "Paths & Logging") with no extra
plumbing - a real admin-page alert is still future work (see
`CLAUDE.local.md`'s TODO list), but the failure is at least captured
reviewably today, same "surface it somewhere, don't let it scroll by"
motivation as `mapping_gaps` (`db/CLAUDE.md`). Its cursor and success key
(`odds_last_success_at`/`odds_prekickoff_last_success_at`, passed to
`_capture_odds()` as `success_key`) follow the same pattern every task
now uses - see "No task can end the tick" above. Nothing schedules off
the `*_last_success_at` keys; they exist for `meta:admin` (see "KV
writer" below).

`_run_pregame_weather_capture()` (added 2026-09-15) queries for any
`SCHEDULED` game within `WEATHER_PREGAME_NEAR_WINDOW_HOURS` (24) of
kickoff to decide which of the two intervals gates it this tick - same
"query `games.game_time` directly rather than hardcode a day/time" shape
as `_run_pre_kickoff_odds_capture()`/`_is_live_window_active()`. Runs
unconditionally every tick (not nested in the live/quiet branch) for the
same reason the pre-kickoff odds capture does: a currently-live early
game shouldn't be able to suppress the forecast refresh for an
approaching later one. No separate KV write needed - `write_incomplete_weeks_games()`
already runs unconditionally at the end of `main()` and picks up
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

`write_current_week_games()` (see "KV writer" below) originally ran
unconditionally at the end of `main()`, live or quiet — same
category of fix as `has_final_stats` above, found the same way. A tick
that observes a game go live→FINAL correctly stops treating it as live
and takes the quiet branch, but `write_current_week_games()` used to
only run unconditionally *inside* the live branch; the games KV key
would then show a stale `IN_PROGRESS`/`live` block for up to 24h (the
next housekeeping run) after a game actually ended. Fixed 2026-09-11 by
moving the call out of `_run_live_updates()` to the bottom of
`main()`, alongside `_run_deadline_sweep()`/`_run_finished_game_stats()` —
cheap regardless (a few small `SELECT`s + one KV write), so no reason to
gate it.

**Replaced with `write_incomplete_weeks_games()` 2026-09-15** — a
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

`write_current_week_leaderboard()` similarly moved to an unconditional
call at the bottom of `main()` (2026-09-15, right after
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

`write_admin_status()` (see "KV writer" below, `meta:admin`) runs
unconditionally as the last thing in `main()`, after `write_season_trends()`
and the user profiles refresh — cheap local reads, and an admin health
check is most useful exactly when something just failed, not stale.

## Live ticker

`src/live_ticker.py` (added 2026-09-27) is the second cron process, next
to `orchestration.py` and also run every minute
(`uv run python -m src.live_ticker [local|prod]`, its own crontab line).
Outside game time it exits after one D1 query
(`game_snapshots_loader.has_candidate_games()`). During games it loops for
most of its minute: a round at 0/15/30/45 seconds, each one
`load_game_snapshots()` (one ESPN scoreboard call for every game) and then
`kv_writer.write_games_weeks()` for just the weeks whose snapshot changed.
No new round starts after `LAST_ROUND_START_SECONDS` (45), so a run ends
before cron starts the next; its lock (`live_ticker.{env}`) covers a
run that doesn't.

Why a separate process: measured live 2026-09-27, the scoreboard in KV ran
up to a minute behind ESPN's clock. Everything refreshed once per cron
minute, the clock came from CBS (which lags ESPN), and Sports IO's clock
sat still for 2+ minutes at a time. The minute tick itself takes 40+
seconds on a game day, so looping inside it wasn't an option. ESPN has no
quota and one call covers every game, so 4 calls a minute costs nothing
against any API budget; Sports IO, CBS and The Odds API aren't touched.
KV cost is about 3 extra games-key writes a minute during games.

It sets the same `game_snapshot_last_capture_at`/`game_snapshot_last_success_at`
cursors orchestration used to, so `meta:admin`'s `game_snapshot_capture`
still reports it - if that goes quiet during a game, the ticker's cron
line isn't running. Failures are `soft()` like everything else
(`game_snapshot_capture`, `live_games_kv_write` in `system_events`). Both
processes can write the same week's games key in the same second, over
KV's one-write-per-second-per-key limit, so `KVClient.write()` retries a
429 once after `RATE_LIMIT_RETRY_SECONDS`.

The games key's top-level `home_score`/`away_score` prefer the snapshot's
(ESPN) score while a game is live, when it's ahead of the `games` row's
(Sports IO) - see `_prefer_snapshot_score()`. D1's `games` row itself
stays Sports IO's.

What's left between a play and a viewer's screen is on the UI side: KV
itself can take up to about 60 seconds to reach every Cloudflare location,
and the web app's own polling interval. See the UI-changes Artifact linked
from `CLAUDE.local.md`.

## KV writer

`src/kv_writer/` computes derived JSON blobs from D1 and writes them to
Cloudflare KV for `cbs-pickem-web`'s Worker to read — D1 stays the system
of record, KV is a serving cache (see root `CLAUDE.md`'s Commands list
and `CLAUDE.local.md`'s "Web UI" section for the overall architecture
decision). Eleven key types; six (games, game details, leaderboard, odds,
week trends, recap) have a `write_week_*`/`write_current_week_*` pair (the
latter is a one-line call to `shared.for_current_week(write_week_*,
label)`, which resolves `weeks.is_current` via `resolve_current_week()`,
warns and skips if there isn't one, then delegates).

**Split into one module per key, 2026-09-23** (was a single 1400+ line
`src/kv_writer.py`): `games.py`, `leaderboard.py`, `odds.py`, `trends.py`,
`recap.py`, `historical.py`, `user_profiles.py`, `admin.py`, plus
`shared.py` for the handful of things genuinely used across more than one
of those (`GAMES_SQL`/`PICKS_SQL` — the literal same query used by both
`games.py` and `trends.py`, not duplicated; `resolve_current_week()` and
`for_current_week()`;
`game_team_dicts()`/`split_home_away()`; `write_meta_current()` itself, since `meta:current` is
just `resolve_current_week()` plus two pool-rule constants and the CBS
pool link, not worth its own file). The pick'em rules for one game -
`favorite_side()`, `winner_side()`, `ats_side()`, `pick_side()`,
`other_side()` - and `standard_rank()` live outside kv_writer in
`src/game_rules.py` (2026-09-29), which imports nothing from the project,
so `src/user_stats.py` (imported by kv_writer) uses the same rules
instead of keeping its own copies. Two further cross-module dependencies were kept as direct
imports rather than folded into `shared.py`, since each is really owned by
one domain that the other legitimately depends on: `trends.py` imports
`odds.py`'s `open_close_consensus_by_game()` for its spread/total movers
(so the movers and `week:*:odds`'s own open/close numbers can never
disagree), and `user_profiles.py` imports `historical.py`'s
`career_record_by_user()`. `__init__.py` re-exports every public
`write_*`/`compute_week_leaderboard` name so `orchestration.py`'s and
`season_close_out.py`'s existing `from src.kv_writer import ...` lines
didn't need to change. The CLI entry point (`main()`, the
`if __name__ == "__main__":` block) lives in `__main__.py`, not
`__init__.py` — `python -m src.kv_writer` runs a package's `__main__.py`
unconditionally, never `__init__.py`, regardless of what guard is written
there (confirmed live: `__init__.py` alone raises `No module named
src.kv_writer.__main__`).

- `write_meta_current()` → `meta:current` — `current_week` from
  `weeks.is_current`, plus `second_half_start_week` and `paid_places`
  (both config, see below) so the UI never hardcodes pool rules, and
  `cbs_pool_url` (added 2026-09-27, `api.cbs_client.cbs_pool_url()` +
  `CBS_POOL_ID`, the same builder `CBSClient` scrapes from) for the UI to
  link out to the CBS pool home page.
- `write_week_games()` → `week:{season}:{weekNN}:games` — schedule +
  picks (naturally empty pre-lock, `user_picks` only ever has
  locked/revealed rows) + a `live` block (down/distance/possession/
  weather from `game_snapshots`) present only while `games.status` is
  `IN_PROGRESS`/`HALFTIME`/`DELAYED` — a missing `live` key means no live
  data, not zeros. Snapshots are only captured for `IN_PROGRESS`/
  `HALFTIME`, so during a delay `live` is the last snapshot from before it. Also carries `stadium` (name/city/state/country/lat/lng/
  roof_type/surface_type, `None` if `games.stadium_id` isn't resolved
  yet) and `forecast` (added 2026-09-15, `games.forecast_*` — the
  pregame forecast captured by `src/loaders/pregame_weather_loader.py`,
  `None` until a capture has happened, permanently `None` for
  enclosed-roof stadiums) on every game, independent of live status -
  unlike the `live` block's weather, this is meant to be visible
  *before* kickoff (the actual point of it - helping a pick get made
  with the forecast in mind), and simply stops updating once a game goes
  live rather than disappearing. `forecast.during_game` (added
  2026-09-27, `games.forecast_window_*`) summarizes the first three
  hours after kickoff, so rain/wind rolling in mid-game shows up even
  when the kickoff hour itself looks fine. `forecast.source` is `"hourly"` or
  `"daily"` (see `pregame_weather_loader.py` above) so the UI can label a
  coarser day-level forecast. `during_game.hours` (added 2026-09-27,
  `games.forecast_hours_json`) is every hourly entry in that window
  (time/temp/condition/icon/precip/wind, chronological) so the UI can
  show which way it's trending - the aggregates beside it are computed
  from exactly these entries, kept for at-a-glance use. Empty for a
  daily-source forecast. Stored as one JSON text column rather than a
  child table since it's only ever read whole and replaced every capture. Each of `home_team`/`away_team` also
  carries a `record` (`{wins, losses, ties}`, added 2026-09-15 from
  `teams.wins`/`losses`/`ties` — see `src/loaders/teams_loader.py` above
  — `None` if that team hasn't synced a record yet). This is the team's
  *current* record as of the last daily housekeeping sync, not the
  record as it stood entering that specific game - no per-week history
  kept, same reasoning as the pregame forecast overwriting in place.
  `neutral_site` (added 2026-09-26, `games.neutral_site` from
  `src/loaders/espn_loader.py`) flags international and domestic
  neutral-site games alike; `stadium.country` is what tells the two
  apart if the UI ever needs to.
  Scoreboard additions (2026-09-27): `linescore` (`{home, away}` each
  `{q1, q2, q3, q4, ot}` from `games.*_qN_score`/`*_ot_score`, `None`
  before kickoff, unplayed quarters null). The team box score was briefly
  here too but moved to the per-game details key the same day (see
  below) to keep this key to what the scoreboard shows. The `live`
  block also carries `yard_line` and `possession_text`. `yard_line` is
  ESPN's `situation.yardLine` as-is: yards from the **home** team's goal
  line (0-100) regardless of possession - confirmed live against ESPN's
  drive log (SF home: "SF 20" → 20, "ARI 27" → 73). Safe to read in our
  home/away frame since `espn_loader` only links an event whose home/away
  abbreviations match ours. ESPN's 0 ("no spot") is emitted as null. At
  halftime/between quarters it's still the last spot with `possession`
  null, so the UI should key the ball marker off possession, not just a
  non-null `yard_line`. `scoring_plays` (added 2026-09-27) is every
  `game_scoring_plays` row for the game, chronological (`quarter`/`clock`/
  `team_id`/`type`/`description`/`player_name` and the score *after* the
  play) - `[]` until someone scores. With `cbs_spread` it gives the exact
  point the cover flipped. Also `last_play` (`{text, type}`), `drive_text`
  and `win_probability` (`{home, away}`, 0-100), all from ESPN's
  scoreboard `situation.lastPlay` (the same response already polled for
  down/distance - no extra endpoint), stored per snapshot in
  `game_snapshots.last_play_*`/`drive_text`/`*_win_pct` so win
  probability can be charted over time (live, sampled per snapshot - the
  complete per-play curve is in the details key once FINAL). `drive_start`
  (`{yard_line, text}`, added 2026-09-27, `game_snapshots.drive_start_*`)
  is where the current drive began, from `situation.lastPlay.drive.start`
  on the same scoreboard response, in the same home-goal-line frame as
  `yard_line` (confirmed live: "LAR 37" → 63 with DEN home). ESPN reports
  it as state, not something we build up from plays, so a missed poll or
  play can't leave it wrong. Right after a change of possession
  `lastPlay` can still be the old drive's last play until the new drive's
  first snap. `leaders`
  is the top passer/rusher/receiver per team by yards (same line shape
  as the details key's players, `None` until player stats exist).
- `write_game_details(game_ids)` → `game:{season}:{game_id}:details`
  (added 2026-09-27, `src/kv_writer/game_details.py`) — everything about
  one game the scoreboard itself doesn't need, ~50KB a game, only fetched
  when someone opens a game: `{game_id, updated_at, box_score, players,
  win_probability}`, each part `None` until its data exists (a game with
  none of them is skipped, not written empty).
  `box_score` is `{home, away}`, every `game_team_stats` column minus its
  ids, plus `punts`/`punt_yards`/`punt_average` (added 2026-09-27) summed
  from that team's `Punting` player lines, since Sports IO's team stats
  have no punting. `None` until player stats exist, 0 punts (average
  `None`) for a team that never punted. Average rounds half-up to match
  Sports IO's own per-punter `average` (checked: all 94 team sides on
  prod agree). `players` is `{home, away}`, each a `{group: [player lines]}` map
  (group keys lowercased: `passing`, `kick_returns`, ...), each line
  `{name, sports_io_player_id, image, stats}`, ranked by that group's
  main stat (yards, tackles for `defensive`, points for `kicking`).
  `win_probability` is ESPN's full per-play curve from
  `game_win_probability`, only once the game is FINAL: chronological
  `{period, clock, home_win_pct, home_score, away_score, scoring_play}`,
  starting with a pre-kickoff point (period 0, no clock, 0-0).
  Written write-through for changed games only, via orchestration's
  `_write_game_details()` (soft-fail) from the live team stats, live
  player stats, finished-game and win-probability steps;
  `write_week_game_details`/`write_current_week_game_details` for a full
  refresh (the latter is in `python -m src.kv_writer`). Weeks 1-3 of 2026
  written to prod 2026-09-27 (an earlier `game:{season}:{game_id}:players`
  key from the same day was superseded and deleted).
- `write_week_leaderboard()` → `week:{season}:{weekNN}:leaderboard` —
  cumulative/first-half/second-half scores and tie-aware `place`
  (`game_rules.standard_rank()`, standard competition ranking: ties share a place,
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
  `open_close_consensus_by_game()` from `write_week_odds()` above so the
  movers and the odds key's own open/close numbers can't disagree). No
  `write_current_week_trends()`-only wrapper distinction worth calling out
  beyond the usual pair — it's the same shape as `write_week_games()`/etc.
  Also carries `lone_geniuses`/`lone_fools` (added 2026-09-23) — the graded
  subset of `all_alone` (which now carries a `correct` field on every
  entry, computed via `_ats_side()` the moment its game goes FINAL, `None`
  before that): a "genius" went alone against a real crowd and covered, a
  "fool" went alone and didn't. Both sorted by `opposing_count` descending
  — biggest crowd defied first — specifically so a UI can grab index `[0]`
  of either list for a one-line weekly headline without any client-side
  filtering.
- `write_season_trends()` → `season:{season}:trends` (added 2026-09-13,
  same commit/doc-gap as above) — season-long pick totals per team, teams
  nobody in the pool has picked all season, each team's ATS cover record
  computed straight from `cbs_spread` + final scores via `_ats_side()`
  (independent of whether the pool ever actually picked that team, unlike
  the pick-popularity numbers), and every "all alone" pick logged all
  season. Shares `split_home_away()`/`_all_alone_entries()`/
  `game_team_dicts()` helpers with `write_week_trends()`. Has no
  `_current_week`-resolving wrapper since it isn't scoped to a week at
  all — called directly from `orchestration.py`. Also carries
  `spread_analysis` (added 2026-09-23, `_spread_bucket_trends()`) —
  compares straight-up pick accuracy (picked the actual winner) against
  ATS pick accuracy (picked the side that covered `cbs_spread`) bucketed
  by spread size (`0-3`/`3-7`/`7-14`/`14+`, hand-picked cutoffs same as
  the other trend thresholds), both dimensions computed straight from
  `_ats_side()`/final scores rather than trusting `user_picks.is_correct`
  — same source of truth `team_ats_record` above already uses, so a pick's
  ATS correctness can't disagree with a team's own cover record. Each
  bucket carries `overall`/`home_picks`/`away_picks` accuracy splits, plus
  a separate `by_team` breakdown (same accuracy pair, per team per
  bucket) — answers "does picking the spread actually differ from picking
  to win, and at what spread size," the thing prompting this in the first
  place (2026-09-22 discussion — the pool grades against the spread, not
  straight-up winners, so this was previously unanswered from any KV key).
  Also carries two more additions from the same 2026-09-23 discussion:
  `trap_team` (`_trap_team_ranking()`, renamed from an initial
  `public_enemy` the same day — "trap team" is the actual sports-betting
  term for this) — every team ranked by `trap_score = pct_of_all_picks ×
  (1 − cover_pct)`, a popularity-weighted badness score rather than a hard
  `cover_pct < .5` cutoff (which would return nothing early in a season
  when sample sizes are thin) — a high score means the pool loves this
  team and it's burning them, not just "unpopular and bad" or "popular but
  fine." And `team_believers_faders`
  (`_believers_and_faders()`) — per team, splits every pick made in one of
  that team's games into believers (picked this team) vs faders (picked
  the opponent), each with its own ATS accuracy; a believer's pick is
  correct exactly when this team covered and a fader's is correct exactly
  when it didn't (the same boolean either way, since there are only two
  sides), so it can never disagree with `team_ats_record`'s own
  `cover_pct`. Sorted by how far apart the two groups' accuracy is, so the
  most divergent (and most interesting) teams sort first.
- `write_week_recap()` → `week:{season}:{weekNN}:recap` (added
  2026-09-28, `recap.py`) - short rotating "did you know" items for the
  UI's weekly infographic: `{version, season, week, updated_at,
  week_complete, games_final, games_total, items: [...], series:
  {pool_accuracy, chaos}, movers, cover_streaks}`. Each item is `{id,
  kind, category, scope (week|season), score, headline, short,
  sample_size, data}`, sorted by `score` descending; the UI rotates
  through the top few and can render `headline` (or `short`, at most
  `_SHORT_MAX` (80) characters, for a one-line strip) as-is or build its
  own from `data`. `id` is unique within the key (`kind` plus a suffix
  when a kind can appear more than once). Everything is "as of" the
  key's week (season data through that week only). `version`
  (`SCHEMA_VERSION`) is bumped whenever a kind is renamed/removed or a
  field changes shape, so the UI can catch a stale card mapping (it went
  through this once with `public_enemy` → `trap_team`) - 3 as of
  2026-09-29, when the key was renamed from `:tidbits` and its
  `tidbits` list to `items`. Every person anywhere in the key is `{user_id, name}`
  (`_person()`), since the UI matches on id. `movers` is every
  leaderboard move of `_MIN_RANK_MOVE` (3)+ places vs last week and
  `cover_streaks` every active team streak of 3+ (`{team, streak_type,
  length}`) - the `biggest_mover`/`cover_streak` items only headline
  the biggest, these feed row arrows and game badges. Categories: `pool`,
  `spread`, `crowd`, `chaos`, `users`, `teams`, `league` (the four
  league-wide cover kinds - their ids keep an older `:league` suffix) and
  `splits` (the pool's own splits).
  Always-on kinds (hand-picked base score, higher when the week is
  extreme): `pool_accuracy` (CBS's own `is_correct`, active users),
  `perfect_week`/`winless_week` (all 5 picks graded), `spread_mattered`
  (week and season: the straight-up winner didn't cover, plus how many
  pool picks had the winner and still lost), `crowd_record` (week and
  season, the side more of the pool took in each game, ATS, with the
  fade-the-crowd inverse) and `popular_picks` (week and season, only crowd
  sides picked by `_POPULAR_POOL_SHARE` (30%, rounded up - 10 of 33) of
  that week's pool, plus the week's most-picked team. Pool size is that
  week's `weekly_performance` rows for active users, not whoever's picks
  are visible so far, which before the Sunday deadline is a handful. A
  share rather than a flat count so it holds if the pool size changes,
  and rather than a top 3, which ties at the cutoff most weeks (week 3:
  three teams at 11) and always returns 3 even in a spread-out week.
  Renamed 2026-09-28 from `consensus_record`/`consensus_locks`, which
  used an 80% share of a game's pickers and let a 4-1 split count the
  same as 20-5), `chaos_index` (the average of
  underdog cover rate, doubled upset rate, pool miss rate and big-favorite
  (7+) outright losses, as 0-10 - a part with nothing to measure is left
  out rather than scored 0. Since 2026-09-28 also shown for a week in
  progress once `_CHAOS_MIN_GAMES` (8) are final, headlined "so far" with
  `partial: true`. A complete week is ranked against the season's other
  complete weeks; a partial one only gets an "on pace for" claim once
  `_CHAOS_PACE_MIN_GAMES` (13) are final - replaying weeks 1-3, the index
  was off by up to 2 points after 9-10 games but within about half a
  point after 13-14), `twins` (identical 5 picks), `oppos` (same 5
  games, every pick opposite), `cover_streak` (active team streaks of
  3+, a push ends one), `biggest_mover` (cumulative rank change vs last
  week, ranked like the leaderboard, 3+ spots), `upset_of_week`
  (biggest-spread underdog to win outright, and who had them).
  Split kinds only appear when they clear `_stands_out()`: a floor on
  sample size (`_STANDOUT_MIN_*`) and a binomial z-score of at least
  `_STANDOUT_MIN_Z` (1.5) against a coin flip. Their score is that z,
  capped at `_STANDOUT_MAX_SCORE` (3) since z grows with sample size and
  would otherwise bury every weekly item by midseason. Kinds:
  `home_road_covers`, `favorite_covers`, `home_underdog_covers`,
  `division_underdog_covers` (league-wide), `pool_split` (the pool's ATS
  record picking home/road, favorites/underdogs, by kickoff slot, in
  division games) and `team_split` (one team's ATS in primetime, division
  games, at home, on the road - rarely qualifies before midseason).
  Neutral-site games skip anything home/road. Kickoff slots are Eastern
  time: the weekday (Tuesday through Saturday, Monday - 2026's opener was
  a Wednesday), with Sunday split into morning (before noon,
  international), early (before 4), late (before 7) and night; primetime
  is Wednesday, Thursday, Sunday night and Monday. Pool/team splits grade
  with `ats_side()` like `season:trends`; only `pool_accuracy`/perfect/
  winless use CBS's grade. The pool's favorite-pick share is expected to
  be lopsided, so it rides along as `favorite_pick_share` on the
  favorite/underdog `pool_split` data instead of being an item (it
  briefly was, and topped every week at z = 9). Thresholds and base scores
  are judgment calls, like the trends thresholds.
  Written by orchestration every `RECAP_INTERVAL_SECONDS` (5 min, live
  or quiet, `recap_write` in `meta:admin` with a stale flag) rather than
  every tick - it's an infographic, not a live number - via
  `write_recent_weeks_recap()`: the current week, any started week not
  yet complete, and any week whose last kickoff (`weeks.end_time`) was
  within `_RECENT_WEEK_HOURS` (12). Only writing `is_current` (the first
  version) lost a week's final state whenever CBS moved the current week
  on before Monday night's game ended, and CBS's grades land a poll or
  two after a game goes FINAL anyway. Weeks 1-2 of 2026 were backfilled
  by hand 2026-09-28, and weeks 1-4 rewritten under the new key name
  2026-09-29 (`write_week_recap(n)`). UI reference Artifact
  linked from `CLAUDE.local.md`'s recap TODO entry.
- `write_historical()` → `meta:historical` — see `db/CLAUDE.md`'s
  `historical_standings` section for what feeds this.
- `write_user_profiles()` → `user:{user_id}:season:{season}`, one key per
  active user (added 2026-09-21 — never actually documented here until
  now; see `CLAUDE.local.md`'s "`user_stats` has no loader" entry for the
  original write-up). All the real computation is `src/user_stats.py`'s
  `compute_user_profiles()`; this function just supplies each user's
  career record (`historical.py`'s `career_record_by_user()`) and does the
  per-user KV writes. Covers career record, rolling hot streak (weeks at
  ≥80% accuracy), team-pick streak, home/away/favorite/underdog bias
  (season-wide `pct`/`picks` only, no streak - see below),
  contrarian-vs-chalk accuracy, best/worst week, consistency (score
  stddev), clutch (accuracy in each period's deciding week), and four
  team-callout fields forming a 2×2 (picks-for-this-team-only vs
  either-side-of-the-matchup) × (bad vs good) — see below. Full
  field-by-field reference (including which fields are tendency vs
  accuracy — a real point of past confusion) lives in a published
  Artifact, not this file — ask before assuming it's current.

  **`trap_team`/`lucky_team`** (`_team_habit_ranking()`, shared core) —
  weighted by how big a share of this user's graded picks (among teams
  clearing `_MIN_TEAM_PICKS_FOR_RECORD`) went to a team, not just raw
  `win_pct`: `trap_score`/`lucky_score = share_of_picks × (1 − win_pct)`
  or `× win_pct` respectively. A team picked twice and lost both would
  count the same toward a pure-rate metric as a team picked ten times and
  lost eight, but only the second is really a habit that's hurting (or
  helping) them - same shape as the group-level `trap_team` in
  `season:trends` above, but personal. Superseded the original
  `nemesis_team`/`lucky_team` (pure `win_pct`, no volume weighting) on
  2026-09-23, once a real example showed the pure-rate version could rank
  a team that caused 3 losses above one that caused 7, just because the
  3-loss team's *rate* happened to be worse on a smaller sample.

  **`blind_spot_team`/`sweet_spot_team`** (`_team_readability()` +
  `_blind_spot_and_sweet_spot()`, added 2026-09-23) — combined
  believer+fader accuracy per team: picking team P and fading P's
  opponent O are the same real bet (P covers exactly when O doesn't), so
  every graded pick contributes the identical correctness to *both*
  teams' tallies at once. The team with the lowest/highest combined
  accuracy is the one this user reads worst/best regardless of which side
  they take on it - a genuinely different question from `trap_team`
  ("which team's games should I stop picking *for*") vs this ("which
  team's games should I stop picking at all, either way").

  Both pairs share the same guard, all caught from one real example
  (2026-09-21: a user's only qualifying team was undefeated at 2-0, yet
  `min()` was still forced to return it as their own "nemesis"; a
  follow-up pass the same day caught that an exact `.500` team could pass
  `trap_team`'s and `lucky_team`'s guards *simultaneously*, since a raw
  `score == 0` check doesn't catch a nonzero score on both sides).
  `trap_team`/`blind_spot_team` require the record to genuinely be
  losing; `lucky_team`/`sweet_spot_team` require it to genuinely be
  winning; an exact `.500` team (or no qualifying team) reports `None`
  for whichever side, or both, doesn't actually hold. Confirmed live
  against prod for both fixes.
  **`head_to_head` was removed 2026-09-23** — it compared whole-week
  scores between every pair of users (who scored higher that week), which
  turned out to carry no information beyond what the leaderboard already
  shows directly; a pick-disagreement-based replacement was considered but
  dropped in favor of the team-centric `team_believers_faders` above,
  which covers the same "who's actually right when people disagree" idea
  without needing a `user_id` pairing.
  **`pick_bias.*.current_streak`/`longest_streak` were also removed
  2026-09-23** — caught the same day the caveat below is dated: these
  picks are only orderable by each game's `game_time` (kickoff), not the
  user's actual decision order (CBS exposes no per-pick timestamp at all,
  since a pick can be changed anytime before its game locks), and the
  streak calc didn't even reset at week boundaries the way
  `team_pick_streak`/`hot_streak` deliberately do - it could silently
  chain the last pick of one week into the next as if back to back. A
  streak claim that can't be stood behind is worse than no streak claim;
  `pick_bias.*.pct` (season-wide share, no ordering involved) is
  unaffected and is what's actually reliable here.
- `write_admin_status()` → `meta:admin` (added 2026-09-11) — a health-check
  summary for an eventual admin page: when each `orchestration.py` task
  last ran (from `orchestration_state`) plus recent `mapping_gaps`/
  `system_events` rows to review. Every task reports `last_at` (the
  scheduling cursor, moved on every attempt) and `last_success_at` (moved
  only when it worked) - since 2026-09-27 every task has both as separate
  keys (see Orchestration's "No task can end the tick"); before that, the
  tasks without a try/except only set their cursor on success, and their
  new `*_last_success_at` keys were seeded on prod from those cursors.
  Staleness always compares against the success cursor, and is flagged
  for the tasks expected to run regardless of live/quiet state: odds
  (combining its two cursors - the flat baseline and the pre-kickoff
  capture, since either one succeeding recently means odds data is
  fresh), housekeeping, the CBS quiet picks poll, pregame weather, and
  user profiles. Thresholds (`_*_STALE_SECONDS`) are deliberately looser
  than `orchestration.py`'s own intervals, roughly twice each, and not
  imported from there to avoid a circular import (`orchestration.py`
  already imports from this package). The live pollers (Sports IO/CBS
  live polls, live team stats, live player stats, and `game_snapshots`
  from `src/live_ticker.py` - same cursor keys as when orchestration
  captured them) and
  the every-tick scoring plays/win probability steps just report
  timestamps with no stale flag - "should this have run" for those
  depends on live-window history, which isn't worth the complexity.
  Called as the last step of every tick rather than after a specific D1
  write, since it's a handful of cheap local `SELECT`s and freshness
  matters most exactly when something just broke.
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
  **Scoped to weeks that have started, 2026-09-27**: by default only
  incomplete weeks whose `start_time` has passed, plus the current week
  even before its first kickoff. It used to rewrite every future week too
  (16 keys a minute in week 3), most of the pipeline's roughly 900k KV
  writes a month for keys whose data only changes on the daily sync.
  Housekeeping calls it with `include_future=True` once a day to cover
  those.

**Write-through, not polling or diffing**: every write function is
called immediately after the specific D1 write that could have changed
its underlying data (see `orchestration.py`'s call sites), not on its own
timer and not after checking whether anything actually changed. This was
a deliberate design choice over both alternatives — a timer decouples the
write from the actual change (stale between ticks or wasted no-op writes
when nothing changed), and diffing adds a read-before-write for no
correctness benefit since these writes are already cheap and idempotent.
The exceptions run unconditionally every tick instead, because several
different loaders feed each of them: `write_incomplete_weeks_games()`,
`write_current_week_leaderboard()`, `write_current_week_trends()`,
`write_season_trends()` and `write_admin_status()` - see above.

`config.OVERALL_PAID_PLACES`/`FIRST_HALF_PAID_PLACES`/
`SECOND_HALF_PAID_PLACES` (added 2026-09-11, `PAID_PLACES` in
`kv_writer/shared.py`) are hand-set pool-admin rules, same convention as
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
