# cbs-pickem

A little pipeline for my CBS Sports Pick'em pool. Every week each player picks five NFL teams against the spread, and CBS keeps score. We run the contest with first and second half winners in addition to overall. CBS's own site is fine for checking the current/overall standings but to capture second half and other trends I use this to pull everything into a real database and write data to a web UI for displaying: [morlocked.rattsnest.com](https://morlocked.rattsnest.com).

- Logs into CBS and pulls weekly standings + everyone's picks
- Pulls team/schedule/live score data from api-sports.io, odds from The Odds API, and stadium weather from Pirate Weather
- Stores all of it in a Cloudflare D1 database
- Computes leaderboards/trends/game info and writes them out as JSON blobs, which is what the web UI actually reads from
- Runs on a cron schedule to update live games, odds, and weather

The web UI itself lives in a separate repo [https://github.com/RyanThornburg/cbs-pickem-web](https://github.com/RyanThornburg/cbs-pickem-web)

## Running it

Dependency management is via [uv](https://docs.astral.sh/uv/). Requires Python 3.14+.

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
```

`orchestration.py` current runs on a cron every minute and then figures out what needs refreshing (live games vs. off-hours, odds cadence, etc.) rather than needing separate cron entries per job. This helps with API limits/usage and is configurable should those change.

CBS and Sports IO are the only two required APIs. Odds API and weather API are optional and should still work without.

## Layout

- `api/`: clients for each external data source (CBS, Sports IO, The Odds API, Pirate Weather, ESPN)
- `db/`: schema, D1 client, KV client
- `src/loaders/`: pulls data from the `api/` clients and writes it into D1
- `src/orchestration.py`: the scheduler that ties it all together
- `src/kv_writer.py`: turns D1 data into the JSON the web UI reads
- `config/`: env handling and season-level settings

## A note on AI

I used claude code for building out most of the models, schema boilerplates, this readme, etc to save time. I tried to comment where it was used heavily (models) otherwise it's probably obvious where it was used based on the commenting.

## Season boundaries

`config.SEASON` gets bumped by hand once a year, there's no auto-detection of "a new season started." Before bumping it, run `src.season_close_out` to lock in the final standings for the season that just ended. See `CLAUDE.md` for the details on why the ordering here matters.
