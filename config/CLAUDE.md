# config/CLAUDE.md

`config/config.py` centralizes env loading (`load_env('local'|'prod')`) and
exposes one typed getter per concern (`get_d1_config()`, `get_cbs_config()`
— the latter returns a `CBSConfig` dataclass). `get_cbs_config()` calls
`load_env()` itself and raises if it fails, so CBS config is validated at
the point it's requested rather than relying on the caller to check
`load_env()`'s return value. For everything else, call `load_env()` once,
then pull whichever config a module needs — see `db/setup.py` for that
pattern.

`config/config.py` also exposes `get_week_path(week)`,
`get_players_path()`, and `get_pool_home_path(week)` — where scraped CBS
data gets written under `DATA_DIR` (see `api/CLAUDE.md`'s CBS Scraping
section).

`get_d1_config()` returns `dict[str, str]`, not `dict[str, str | None]` —
each `os.getenv(...)` call has an explicit `""` default so the type
checker knows these are always real strings by the time `D1Client(**...)`
consumes them, matching the pattern `get_cbs_config()` already used.
`load_env()`'s own `required_vars` check is what actually guarantees
these are set in practice; the `""` default only exists to satisfy the
type system for the (should-be-unreachable) case where it wasn't called
first.

`load_env()`'s success log (`"Loaded %s environment"`) is `DEBUG`, not
`INFO` — it gets called many times per script run (every `get_cbs_config()`/
`get_sports_io_api()`/etc. call re-invokes it), so at `INFO` it drowned
out everything else in the logs. The failure paths (missing file, missing
vars) stay at `ERROR` since those are rare and actually need attention.

`STATE_PATH` (Playwright's session-cookie storage, used by
`api/cbs_client.py`) lives under `secrets/`, not the project root —
moved there 2026-09-08 specifically so a whole *class* of sensitive files
gets one blanket `.gitignore` rule (`secrets/`) instead of each new one
needing its own entry remembered case-by-case. `secrets/` doesn't exist
on a fresh clone (git doesn't track empty dirs), so `CBSClient.login()`
creates it (`self.state_path.parent.mkdir(...)`) before Playwright tries
to write there — don't rely on the directory existing without that.

`SEASON` stays a hardcoded constant, bumped by hand once a year —
deliberately, even though both Sports IO (`/leagues?current=true`) and
CBS (pool-home page's `season.year`) can report the current season live.
Considered and rejected routing `SEASON` through an env-var/DB fallback
chain: `config.py` is imported broadly (including by scripts that never
touch D1), and a DB-backed lookup would mean import-time network I/O plus
not knowing local-vs-prod until `load_env()` runs — more machinery than a
once-a-year manual edit justifies. What *is* worth having, and exists:
`sports_io_client.get_current_season()` and `cbs_client.get_cbs_pool_home()`
each compare the source's season against `SEASON` and return `None`
(logging a warning) rather than continuing on a mismatch — see
`api/CLAUDE.md` for details. That catches "forgot to bump `SEASON`"
without needing `SEASON` itself to be derived automatically.

Path-anchoring and logging conventions (`PROJECT_ROOT`,
`configure_logging()`) are repo-wide rules — see the root `CLAUDE.md`'s
"Paths & Logging" section, since every module, not just this one, has to
follow them.
