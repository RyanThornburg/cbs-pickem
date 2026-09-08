# config/CLAUDE.md

`config/config.py` centralizes env loading (`load_env('local'|'prod')`) and
exposes one typed getter per concern (`get_d1_config()`, `get_cbs_config()`
— the latter returns a `CBSConfig` dataclass). `get_cbs_config()` calls
`load_env()` itself and raises if it fails, so CBS config is validated at
the point it's requested rather than relying on the caller to check
`load_env()`'s return value. For everything else, call `load_env()` once,
then pull whichever config a module needs — see `db/setup.py` for that
pattern.

`config/config.py` also exposes `get_week_path(week)` and
`get_players_path()` — where scraped CBS data gets written under
`DATA_DIR` (see `api/CLAUDE.md`'s CBS Scraping section).

Path-anchoring and logging conventions (`PROJECT_ROOT`,
`configure_logging()`) are repo-wide rules — see the root `CLAUDE.md`'s
"Paths & Logging" section, since every module, not just this one, has to
follow them.
