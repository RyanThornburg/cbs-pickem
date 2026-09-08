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
`PRAGMA foreign_keys = ON` in the same call, since SQLite has it off by
default per-connection and it can't be set from schema DDL. If you ever add a
new trigger, note that `db/setup.py`'s `_split_statements()` is
`BEGIN`/`END`-aware so trigger bodies (which contain their own `;`) aren't
split into separate statements.
