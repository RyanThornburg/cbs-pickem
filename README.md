# cbs-pickem

A little pipeline for my CBS Sports Pick'em pool. Every week each player picks five NFL teams against the spread, and CBS keeps score. We pay out on overall standings plus segments of the season (first and second half right now), and the payout structure is a setting, so it can change to thirds, quarters, a last place payout, etc. without touching the standings code. CBS's own site is fine for checking the current/overall standings but to capture the segments and other trends I use this to pull everything into a real database and write data to a web UI for displaying: [morlocked.rattsnest.com](https://morlocked.rattsnest.com).

- Logs into CBS and pulls weekly standings + everyone's picks
- Pulls team/schedule/live score data from api-sports.io, odds from The Odds API, and stadium weather from Pirate Weather
- Stores all of it in a Cloudflare D1 database
- Computes leaderboards/trends/game info and writes them out as JSON blobs, which is what the web UI actually reads from
- Runs on a cron schedule to update live games, odds, and weather

The web UI itself lives in a separate repo [https://github.com/RyanThornburg/cbs-pickem-web](https://github.com/RyanThornburg/cbs-pickem-web)

## Running it

Dependency management is via [uv](https://docs.astral.sh/uv/). Requires Python 3.14+ on Linux or macOS (WSL works too). The scheduler uses Python's built-in `fcntl` to stop overlapping cron runs, and `fcntl` doesn't exist on native Windows.

```bash
uv sync
uv run playwright install
```

You'll need a `config/.env.local` (and `.env.prod` once you're pointed at real infra) with CBS login creds, API keys for Sports IO / The Odds API / Pirate Weather, and Cloudflare D1/KV credentials. See `config/CLAUDE.md` for the full list of what's required vs. optional.

A few of the more useful commands:

```bash
# apply the D1 schema
./setup.sh local

# one-time bootstrap for a new season (teams, users, id mapping)
uv run python -m src.new_season local

# the scheduler: this is what actually keeps everything up to date
uv run python -m src.orchestration local

# the live ticker: game-day scoreboard updates every 15 seconds
uv run python -m src.live_ticker local
```

Both run from cron every minute, as two separate entries:

```
* * * * * cd /path/to/repo && uv run python -m src.orchestration prod
* * * * * cd /path/to/repo && uv run python -m src.live_ticker prod
```

`orchestration.py` figures out what needs refreshing each minute (live games vs. off-hours, odds cadence, etc.) rather than needing separate cron entries per job. This helps with API limits/usage and is configurable should those change. `live_ticker.py` only does anything during games: it pulls ESPN's live scoreboard every 15 seconds so the scoreboard isn't limited to once-a-minute updates, and exits straight away the rest of the week. Each takes its own lock, so a run that goes long is never doubled up.

CBS and Sports IO are the only two required APIs. Odds API and weather API are optional and should still work without.

## Testing

```bash
# the whole suite, with coverage - offline, no credentials needed
uv run pytest

# check the real APIs still match our models (opt-in, needs config/.env.local)
uv run pytest -m live
```

The tests run against an in-memory SQLite stand-in for D1 (D1 is SQLite, so the real SQL and schema get exercised) and a fake KV. The API clients replay real responses saved under `tests/fixtures/` (CBS pages are anonymized since the repo is public), so parsing and validation run exactly as they do in production. That covers the loaders, every KV key the web UI reads, and full orchestration ticks end to end.

The `live` run hits Sports IO, ESPN, The Odds API (1 credit) and Pirate Weather, and fails if a response no longer validates or a field we read has gone missing. When that happens, fix the model, then re-save the fixtures with `uv run python -m tests.fixtures.capture_api`. The contract tests in `tests/test_games.py` pin the field names the web UI reads, so changing a KV key's shape means updating those on purpose (and the web UI with it).

CBS's Playwright login/scraping isn't tested on purpose. It doesn't change with the rest of the code, so if it breaks, CBS changed something.

## Layout

- `api/`: clients for each external data source (CBS, Sports IO, The Odds API, Pirate Weather, ESPN)
- `db/`: schema, D1 client, KV client
- `src/loaders/`: pulls data from the `api/` clients and writes it into D1
- `src/orchestration.py`: the scheduler that ties it all together
- `src/live_ticker.py`: the 15-second game-day scoreboard refresh (its own cron entry)
- `src/kv_writer/`: turns D1 data into the JSON the web UI reads, one module per KV key
- `config/`: env handling and season-level settings
- `tests/`: pytest suite, saved API responses in `tests/fixtures/`

## A note on AI

I used claude code for building out most of the models, schema boilerplates, this readme, etc to save time. I tried to comment where it was used heavily (models) otherwise it's probably obvious where it was used based on the commenting.

Ok, since the 2026 season started, I've gone a little more AI heavy. You can tell via the commits if there's concern on AI usage.

## Season boundaries

`config.SEASON` gets bumped by hand once a year, there's no auto-detection of "a new season started." Before bumping it, run `src.season_close_out` to lock in the final standings for the season that just ended. See `CLAUDE.md` for the details on why the ordering here matters.

## Payouts

The pool's payout structure lives in `config.PERIODS`, one entry per standings period:

```python
PERIODS = (
    Period("overall", "Overall", 1, None, paid_places=5),
    Period("first_half", "First Half", 1, 9, paid_places=3),
    Period("second_half", "Second Half", 10, None, paid_places=3),
)
```

`overall` is the season-long standings and always stays. Everything else is up to the pool: two halves today, thirds or quarters later, just a different list. Each period sets how many places get paid (ties at the cutoff all get paid) and whether it also pays last place (`pay_last_place=True`). Last place only counts players who made all five picks every week of that period, so you can't skip a week and coast to the bottom.

Change it at the season bump, alongside `config.SEASON`, never mid-season. Closing out a season saves that season's structure with its results, so the history page still knows a past season was halves even after we switch.
