# db/CLAUDE.md

Both local development and production use Cloudflare D1 (SQLite).
`db/d1_client.py` talks directly to D1's HTTP query API, and that is the
*only* code path for both environments: `config/.env.local` and
`config/.env.prod` just point at different D1 database IDs (a separate dev
database, so local testing never touches real contest data). wrangler/Node
are not runtime dependencies — wrangler is only used ad hoc, outside the app,
to provision each D1 database once (`wrangler d1 create`).

`db/schema.sql` is SQLite dialect. `updated_at` columns are kept current
by `AFTER UPDATE` triggers, since SQLite has no inline syntax for that.
Foreign keys are enforced — `D1Client.batch()`/`.query()` always send
`PRAGMA foreign_keys = ON` in the same call. (D1 actually enforces foreign
keys ON by default already — confirmed live via `PRAGMA foreign_keys;`
with no prior call from this client — so the comment that used to justify
this as "SQLite has it off by default per-connection" doesn't hold for
D1 specifically; the prepend is kept anyway since it's harmless.) If you
ever add a new trigger, note that `db/setup.py`'s `_split_statements()`
is `BEGIN`/`END`-aware so trigger bodies (which contain their own `;`)
aren't split into separate statements.

Every `CREATE TABLE` and `CREATE INDEX` in the file uses `IF NOT EXISTS` —
keep that convention on anything new. `setup.sh`/`db/setup.py` had never
actually been run successfully against a real database until 2026-09-08,
so this is easy to get wrong without noticing: two real bugs only
surfaced the first time schema was re-applied to an already-provisioned
database (missing `IF NOT EXISTS` on `CREATE INDEX`, and a schema comment
with a literal `;` inside it — see below). When writing a schema comment,
double-check it doesn't contain a literal `;` — this has bitten **repeatedly**
(2026-09-08 and again multiple times 2026-09-09, always the same failure
mode: `db.setup`'s `_split_statements()` splits on raw `;` without knowing
about `--` comments, producing an "incomplete input" error that doesn't
point at the actual offending line). If `db.setup` fails with that error
after a schema edit, a stray `;` inside a comment is the first thing to
check, not a real SQL problem.

## Reconciling the same row from two independent sources

`games` and `weeks` both get written by two different loaders (Sports
IO and CBS) that don't know about each other's writes and can run in
either order. A plain `INSERT ... ON CONFLICT(external_id)` isn't enough
here: if Sports IO creates a `games` row first (no `cbs_event_id` yet),
CBS's own `ON CONFLICT(cbs_event_id)` won't match it (`NULL != NULL` for
uniqueness purposes) and would try to insert a second, duplicate row for
the same real-world game instead of updating the first one.

The fix, used for both tables: give the row a **second natural-key
`UNIQUE` constraint** that's stable across sources —
`UNIQUE(week_id, home_team_id, away_team_id)` on `games`,
`UNIQUE(season_id, week_number)` on `weeks` — and chain a second
`ON CONFLICT` clause targeting it (SQLite/D1 supports multiple `ON
CONFLICT` clauses in one `INSERT`, evaluated in order; whichever target
actually matches fires). Confirmed live: whichever source runs first
creates the row and leaves the other source's external-id column `NULL`;
whichever runs second matches via the natural-key conflict target,
backfills its own external id, and both `game_id`/`week_id` values stay
stable across both writes — no duplicate row, no lost data either way.
The same shape applies to `games.espn_event_id` (matched via
`(home_abbrev, away_abbrev)` at the loader level rather than a DB
constraint, since ESPN never creates a `games` row itself — it only ever
backfills onto one that already exists).

## Timestamps are always ISO8601 UTC text

Every `DATETIME` column (`games.game_time`, `weeks.start_time`/
`end_time`) stores a plain ISO8601 UTC string, e.g.
`"2026-09-14T17:00:00Z"` — never a raw epoch integer, and never a
non-UTC offset. This matters because the sources feeding these columns
disagree on both: CBS's raw timestamp is epoch **milliseconds**, Sports
IO's is epoch **seconds** — storing either raw would make `game_time`
values from the two sources silently incomparable (confirmed as a real,
shipped bug before the fix: `sports_io_loader.py` was writing Sports
IO's raw epoch-seconds int directly into `game_time` for a while). Each
loader normalizes at write time (`cbs_loader._cbs_starts_at_to_iso()`,
`sports_io_loader._epoch_seconds_to_iso()`) so every row is
apples-to-apples and plain string comparison/sorting works correctly
regardless of source. If you add a new time-bearing field from a new
source, convert to this same format before it touches the DB rather than
storing whatever the source natively gives you.

`weeks.start_time`/`end_time` are themselves *derived* from `game_time`
(min/max across that week's games) — the reason they need to be full
UTC timestamps rather than bare `DATE`s (an earlier version of this
schema used `DATE`) is that a "date" isn't actually a property of an
instant until you pick a timezone to view it in; a late Sunday/Monday
night game can cross the UTC day boundary and land on the "wrong" date
for a US viewer if the timezone conversion is baked in at write time
instead of left to render time.

`seasons.season_id` is the season's year itself (e.g. `2026`), declared
`INTEGER PRIMARY KEY` with no `AUTOINCREMENT` — not a surrogate id, since
a season's year is already a stable, meaningful natural key and using it
directly avoids a lookup before every insert. This is the one table where
an external/natural id is the PK; everywhere else (`teams`, `games`,
`weeks`, `user_picks`) uses a plain autoincrement PK plus a separate
`UNIQUE` external-id column (`cbs_team_id`, `cbs_event_id`,
`cbs_pool_period_id`, `cbs_pick_id`, etc.) — follow that pattern for new
external ids rather than replacing the PK, unless the id is as
meaningful/stable as a year.

`user_picks.cbs_pick_id` is a `UNIQUE` reference column, not the upsert
key — `ON CONFLICT` there targets `UNIQUE(user_id, game_id)`, since picks
are only ever persisted once a game locks (values can't change after
that), so `cbs_pick_id`'s stability during pre-lock pick-changing doesn't
matter. Confirmed live (2026-09-08) which of CBS's several per-pick id
fields is actually safe to use here — `pickInfo.cbsItemId` is the
*picked team's* `cbsTeamId` (same value shared across every user who
picked that team, definitely not unique per pick), `pickInfo.itemId` is
unique per pick but only populated once the pick reveals, and
`Pick.id` is unique per pick *and* present even before reveal — that's
the one stored as `cbs_pick_id`.

`weekly_performance.weekly_score` is a generated column
(`AS (picks_correct)`) — SQLite computes it automatically and rejects any
`INSERT`/`UPDATE` that tries to write to it directly; leave it out of
column lists and `VALUES`/`SET` clauses entirely. `games.is_complete`
(`AS (status = 'FINAL')`) is the same pattern applied to a derived
boolean instead of a straight alias — prefer this over having every
loader independently compute and write `is_complete` itself, since two
sources doing that independently is exactly how they'd eventually drift
out of sync with each other.

SQLite's `ALTER TABLE ADD COLUMN` cannot add a `UNIQUE` (or `PRIMARY KEY`)
constraint to an existing column — trying it fails. When a schema change
needs a new `UNIQUE` column on a table that already exists in a live D1
database, `DROP TABLE`+re-run `setup.sh` is the only option (fine for
local dev tables with no real rows yet; check row counts first via
`D1Client.query("SELECT COUNT(*) ...")` before dropping anything).

For a non-`UNIQUE` column, plain `ALTER TABLE ... ADD COLUMN` works fine
against a table that already has rows — `db/setup.py` itself doesn't run
these (it only ever applies `schema.sql`'s `CREATE TABLE IF NOT EXISTS`,
which is a no-op against a table that already exists), so a column added
to `schema.sql` after a database's first `setup.sh` run needs its own
one-off `ALTER TABLE` against that already-provisioned database — there's
no migration runner in this repo, so this has been done ad hoc via
`D1Client.batch()` each time (e.g. `games.forecast_*` and
`teams.wins`/`losses`/`ties`, both added 2026-09-15 - applied to local,
**not yet applied to prod**).

## `mapping_gaps` tracks lookup misses for review

Added 2026-09-09 so unmapped external values (a team/week/stadium/user
that a loader's `id_map()` lookup couldn't resolve) get surfaced
somewhere reviewable instead of only ever showing up as a
`logger.warning()` that scrolls off. `src/loaders/loader_helper.py`'s
`mapping_gap_statement(source, entity_type, raw_value, context)` returns
one `(sql, params)` upsert targeting `UNIQUE(source, entity_type,
raw_value)` — first sighting inserts a row, every later sighting just
bumps `occurrences`/`last_seen_at`, so a value that misses on every run
(e.g. Sports IO's `team.id: 0` placeholder for undetermined future
playoff matchups) accumulates one durable row instead of flooding the
table.

Called **next to** the existing `logger.warning()` at a lookup-miss site,
not instead of it — the two serve different audiences (the log is
for tracing a specific run, the table is for spotting a pattern worth
fixing). Not every lookup miss belongs here, though: only log a gap when
the miss is a genuinely *unknown external value* that a correction table
(`ABBREV_CORRECTIONS`-style) could fix. Skip it when the miss is actually
expected/transient — e.g. CBS's `pickInfo.cbsItemId: null` just means
that entry didn't pick that game (see `api/CLAUDE.md`), and a
`games_by_matchup` miss in `odds_loader.py` just means that game hasn't
been loaded yet, not that a team name failed to map.

Gap statements always ride in the same batch as the loader's real writes
(atomic, no extra HTTP round trip) but are tracked in their own list,
never appended directly into the same list used for a `logger.info("Upserted
%d ...")` count — mixing them in was a real bug caught during this
session's own testing (a run that skipped 7 games due to unmapped teams
briefly reported "Upserted 293 games" instead of 272, because 21 gap rows
had been counted as if they were games). The pattern every call site
follows: a dedicated `gap_statements` list, combined with the real
`statements` list only at the `sql_batch_call(statements + gap_statements,
client)` call, with the log line's `len(...)` always reading `statements`
alone.

## `system_events` tracks real failures for review

Added 2026-09-11, same shape/intent as `mapping_gaps` above but for actual
exceptions (e.g. a failed odds capture) rather than lookup misses -
`UNIQUE(source, message)` so a persistent failure accumulates one durable
row (`occurrences`/`last_seen_at` bumped) instead of flooding the table.
Written next to the existing `logger.exception()` at each catch site, not
instead of it - the log has the full traceback for debugging, this table
is the queryable "is anything broken" summary `src/kv_writer.py`'s
`meta:admin` key surfaces (see `src/CLAUDE.md`'s KV writer section).
First (only, as of this writing) call site: `orchestration.py`'s
`_capture_odds()`, via a small `_record_system_event()` helper kept local
to that module rather than added to `loader_helper.py` - orchestration is
a different layer than the loaders that module serves, and there's only
one call site so far to justify a shared abstraction.

## D1Client gotchas (confirmed live, not assumed from docs)

`db/d1_client.py`'s `batch()` POSTs a single JSON object shaped
`{"batch": [{"sql": ..., "params": [...]}, ...]}` to D1's REST `/query`
endpoint — **not** a bare JSON array of statements, which D1 rejects
outright ("Expected object, received array"). This form supports
per-statement `params` *and* atomicity (a failing statement rolls back
earlier ones in the same batch) — joining multiple statements into one
`;`-separated `sql` string instead does **not** support this, since D1
rejects any `params` at all once a `sql` string contains more than one
statement ("params with multiple statements is not supported").

`_bind_params()` coerces Python `bool` to `int` before every request,
because D1 binds a JSON `true`/`false` as the *literal text*
`'true'`/`'false'`, not SQLite's native `0`/`1` — confirmed via
`typeof(col)` on a real write. Left un-coerced, this silently breaks any
`WHERE some_bool_col = 1`-style query on every `BOOLEAN` column in the
schema, not just whichever one first triggers it — always pass real
Python `bool`s into `D1Client.batch()`/`.query()` params and let
`_bind_params()` handle the conversion, rather than pre-converting to
`0`/`1` at each call site.

D1 supports `INSERT ... RETURNING` — confirmed live 2026-09-10
(`src/historical_backfill.py`'s new-user insert uses it to get the fresh
`user_id` back in the same round trip rather than a follow-up `SELECT`).

## `weeks.is_current` / `seasons.historical_data_incomplete`

`weeks.is_current` (added 2026-09-10) mirrors CBS's own
`poolPeriod.isCurrent` flag — `cbs_loader.load_cbs_weeks()` writes it for
every period on every run (CBS reports `false` for all but one, so no
separate "clear the old current week" step is needed). This is what
`kv_writer._resolve_current_week()` reads to answer "which week is live
right now" instead of guessing from `game_time`.

`load_cbs_weeks()` also corrects `seasons.name` from CBS's real pool name
(e.g. `"MorLocked 10.0"`) every time it runs — `season_loader.py`
(Sports IO-sourced) has no way to know this and defaults to a generic
`"{year} Season"`, which is wrong for anything downstream that expects
the branded name (`historical_standings.pool_name`, `meta:historical`).

`seasons.historical_data_incomplete` flags a season whose *archived*
standings are known to be missing entries — not a property of the season
itself. Confirmed for 2015 and 2016: the saved CBS export for those years
has no rank-1 entry at all (2016 is also missing rank-2), meaning
whoever actually won isn't recoverable from what was saved. Don't assume
a low `best captured rank is N > 1` in a query means those users somehow
tied for the top — it means the winner's row is simply absent.

## `historical_standings` / `historical_user_mapping`

Added 2026-09-10 for a historical winners/standings page, backfilled once
from `data/{year}/{year}_standings.json` (2013-2025) via
`src/historical_backfill.py` (see `src/CLAUDE.md` and root `CLAUDE.md`'s
"End of season" section for how this gets extended going forward).
Deliberately a separate table from `user_stats` rather than backfilled
into it — `user_stats`'s other columns (streaks, home/away splits) are
`NOT NULL DEFAULT 0` and need real per-pick data this archive doesn't
have; storing a false `0` there for "we don't know" would be worse than
not having the column at all.

`historical_standings.first_half_rank`/`first_half_score`/
`second_half_rank`/`second_half_score` are nullable for the same reason,
one level more specific: almost none of the archive has the per-week
`periodScores` breakdown needed to derive a half-season split at all
(only 2024/2025 saved it, and even then nothing parses it automatically,
see below). Confirmed live 2026-09-11 that **`data/2024/2024_standings.json`
and the original `data/2025/2025_standings.json` were byte-for-byte
duplicates** (same pool id, same entries) - a real data-collection bug in
the archive itself, not a code bug. The user resupplied the real 2025
data by hand; re-ran the backfill after clearing the wrong season-2025
rows first (`ON CONFLICT` upserts alone weren't enough, since some users
in the wrong data don't appear in the real data at all and would've been
left as stale rows). Worth a similar spot-check (compare pool ids /
top entries across adjacent years) if any other archived year is ever
suspected of being wrong - the other 12 files were diffed and are each
distinct, so this looks isolated to 2025, but wasn't independently
verified beyond the pool-id comparison.

2024's own first/second-half splits were **not** backfilled automatically
even though its `periodScores` data could technically support deriving
them (mapping each `poolPeriodId` to a week number, then summing on
either side of `config.SECOND_HALF_START_WEEK`) - deliberately deferred
as real extra scope for one year of benefit; only 2025's halves are
filled in, entered by hand from the user's own records. Come back to this
if 2024's half-season winners are ever wanted.

`historical_user_mapping` is the audit trail for how each archived CBS
entry got matched onto a `users.user_id` - name matching is
case-insensitive (confirmed live: "Bill Morlok" in the 2015 archive vs.
`users.name = "bill morlok"` resolved to the same person automatically),
and a name with no match at all gets a brand-new `is_active = FALSE`
user row, logged as a warning for review rather than silently guessed.
