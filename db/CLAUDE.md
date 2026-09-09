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
double-check it doesn't contain a literal `;` — this has bitten twice now
(2026-09-08), since `_split_statements()` splits on raw `;` without
knowing about `--` comments.

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
column lists and `VALUES`/`SET` clauses entirely.

SQLite's `ALTER TABLE ADD COLUMN` cannot add a `UNIQUE` (or `PRIMARY KEY`)
constraint to an existing column — trying it fails. When a schema change
needs a new `UNIQUE` column on a table that already exists in a live D1
database, `DROP TABLE`+re-run `setup.sh` is the only option (fine for
local dev tables with no real rows yet; check row counts first via
`D1Client.query("SELECT COUNT(*) ...")` before dropping anything).

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
