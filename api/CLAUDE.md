# api/CLAUDE.md

## CBS Scraping

`api/cbs_client.py`'s `CBSClient` class holds all CBS-pool-specific state — credentials and the weekly/player/pool-home/login URLs derived from `CBSConfig` — resolved once in
`__init__` rather than as module-level globals computed at import time.
Its public surface is `login()`, `fetch_weekly_data()`,
`fetch_user_data()`, and `fetch_pool_home_data()`; the private
`_fetch_common_pool(url, required_key)` does the actual
fetch-and-extract-or-raise, shared by all three.

Three module-level functions build a `CBSClient` and drive it end-to-end
(fetch, validate against a pydantic model, log, write to disk):
`get_cbs_weekly(week=0)`, `get_cbs_users()`, and `get_cbs_pool_home(week=0)`.
`__main__` calls `configure_logging()` then `get_cbs_weekly()`;
`get_cbs_users()`/`get_cbs_pool_home()` have no CLI entry point yet — call
them directly (`from api.cbs_client import get_cbs_users, get_cbs_pool_home`).

Transient fetch failures (network errors, 5xx/429) are retried with
backoff via `stamina` in `_fetch_html_data()` — but login is deliberately
*not* auto-retried, since repeated failed logins risk tripping CBS's
anti-bot/lockout defenses, so `_credential_login()` fails fast and logs
instead.

All three pages CBS serves (weekly standings, pool players, pool home)
embed the same SSR Apollo transport shape; `_extract_common_pool(html,
required_key)` finds the payload that actually has `required_key`
(`"poolPeriod"` for weekly, `"members"` for players, `"season"` for pool
home), since CBS pushes one Apollo entry per query on a page and
`required_key` is how the code tells them apart. `api/cbs_models.py`'s
module docstring documents the actual CBS payload shape these models parse
(a denormalized `commonPool` response, not the `__APOLLO_STATE__`
normalized cache its typenames might suggest) — including that CBS reuses
the same GraphQL `__typename` for different shapes depending on which
page/query returned it (e.g. `Member.email` is only ever populated when the
payload came from the players page).

`_fetch_common_pool()` retries once via a fresh login if the page loads
(a real HTML response, not empty) but just doesn't contain `required_key`
— e.g. a stale session getting redirected somewhere else. This is
separate from `_read_cbs_source_data()`'s own retry, which only covers
the empty-response case; a 200 response that's simply the wrong page
needs its own retry path, added 2026-09-08 after this exact failure mode
showed up in production logs once (not reproducible on demand, but the
gap was real and confirmed by tracing the code, not guessed at).

Each scrape's raw JSON is saved under `config.config.DATA_DIR`
(`data/<season>/`, season from `config.config.SEASON`): weekly standings at
`data/<season>/Week<NN>/cbs_week_<NN>.json`, pool members at
`data/<season>/players.json`, pool-home data at
`data/<season>/Week<NN>/cbs_pool_home_<NN>.json`.

### Pool-home page: richer per-event/team data than weekly-standings

`fetch_pool_home_data()`/`get_cbs_pool_home()` hit the pool's *home* page
(`https://picks.cbssports.com/football/pickem/pools/<pool_id>`, distinct
from `.../standings/weekly` and `.../players`) — confirmed live to return
substantially more per-event and per-team detail than the weekly-standings
page: `season` (id/year), `oddsMarket` (CBS's own book, current *and*
opening spread/total/moneyline — the "opening vs closing line" data
`CLAUDE.local.md`'s project goals ask for), `extra` (pick-ownership
percentages — the "most-picked teams" trends data), `weekNumber`, `winningTeamId`,
`tvNetworks`, and full team detail (colors, market/nickname, record,
rank) vs. weekly-standings' bare `{id, cbsTeamId, abbrev}`. It has **no**
`standings`/ranked-entries — that's weekly-standings-only. Modeled as
`FootballPickemPoolHome`/`PoolHomePoolPeriod`/`PoolHomePoolEvent` in
`cbs_models.py`, each extending the weekly-standings base class rather
than duplicating it (`Team` itself just got the extra fields added as
optional, since none of them are ever *required* on one page and absent
on the other — only split into a subclass when a field's required-ness
actually differs between the two pages, as it does for `PoolHomePoolEvent`'s
`week_number`/`season_type`/`away_team_id`/`game_status`).

Its endpoint-level `coverage.standings` flag (from a season's metadata)
is **not** a reliable signal for whether `/standings` actually has usable
data — confirmed live that `/standings` returns real per-team data with
`coverage.standings: false` for the season. Don't gate anything on it.

`ABBREV_CORRECTIONS` (in `cbs_client.py`) maps the 2 known cases where
CBS's team abbreviation differs from Sports IO's/`teams.abbreviation`
(`JAC`→`JAX` Jacksonville, `LAR`→`LA` the Rams) — confirmed live by
diffing the full 32-team abbreviation sets from both sources. Used by
`cbs_loader.map_cbs_to_sports_io()` when matching CBS teams onto existing
`teams` rows.

### Both Sports IO's and CBS's "current season" checks fail closed

`sports_io_client.get_current_season()` and `cbs_client.get_cbs_pool_home()`
both compare the source's season year against `config.SEASON` and return
`None` (not the mismatched data) if they disagree, logging a warning —
deliberately chosen over just warning-and-continuing, since a season
mismatch means every write downstream would be tagged with the wrong
year. `get_cbs_pool_teams()` (which calls `get_cbs_pool_home()`) handles
that `None` by returning `[]` rather than crashing.

### CBS pick-data shapes, confirmed against real (including historical) data

`entry.picks` on a `FootballPickemWeeklyStandingsEntry` comes back in one
of three shapes depending on the entry, confirmed live against this
season's real Week 1 data:

- **`[]`** (empty) — this user hasn't made any picks yet.
- **16 items**, every one `displayStatus: "LOCKED"` with `pickInfo: None`
  — this user *has* made at least one pick, but CBS returns a full
  16-slot (one per game that week) placeholder array specifically to hide
  *which* games without hiding *that* they picked something.
- **5 items** (or however many were actually picked), `displayStatus:
  "VISIBLE"` with `pickInfo` fully populated, **even before any games
  have started/locked** — but only for `entry.isMine == True` (the
  logged-in user's own entry). `cbs_loader.load_cbs_user_picks()`'s
  filter (`pick.cbs_slot_id in locked_game_cbs_ids and pick.pick_info and
  pick.display_status != "LOCKED"`) exists specifically to strip this
  self-leak out before persisting anything — confirmed live that without
  the game-locked check, the logged-in user's own not-yet-public picks
  would otherwise leak into what gets saved.

Because of the second bullet, `len(entry.picks) > 0` (for any entry other
than your own) is a reliable, already-public pre-lock signal for "has
this user made picks this week" — safe to use for a UI reminder without
needing to wait for lock or reveal any pick contents.

A pick's `pickInfo` has three id-shaped fields that look interchangeable
but aren't — confirmed by inspecting a real historical week
(`data/example.json`, its `__APOLLO_STATE__` normalized cache) with 170
real locked picks:

- **`pickInfo.cbsItemId`** — the *picked team's* `cbsTeamId`. Only 28
  distinct values across 170 picks, and confirmed identical across two
  different users who picked the same team for the same game — this
  identifies the team, not the pick.
- **`pickInfo.itemId`** — unique across all 170 picks, but only
  populated once the pick reveals (`None` before lock).
- **`Pick.id`** (the pick record's own top-level id, not inside
  `pickInfo`) — also unique across all 170, and present even before
  `pickInfo` populates. This is the one stored as `user_picks.cbs_pick_id`.

`pickInfo.pickStatus` only ever takes 3 values, confirmed across a full
completed historical week: `"CORRECT"`, `"INCORRECT"`, and `"NONE"`
(before the game finishes) — `cbs_loader._pick_status_to_correct()`
maps these to `True`/`False`/`None` for `user_picks.is_correct`.

## Sports IO & The Odds API Clients

`api/sports_io_client.py` (api-sports.io, NFL stats/odds) and
`api/the_odds_api_client.py` (The Odds API, spreads/totals odds only —
that's the one endpoint this app needs from it) are plain
`requests`-based REST clients, architecturally unrelated to CBS's
Playwright-driven scraping above. Neither persists to disk yet — each
`get_*()` fetches, validates against pydantic models, logs, and returns.

They share plumbing from `api/api_helper.py`: a small exception hierarchy
(`ApiError` base, `ApiRateLimitError`, `ApiServerError`, `ApiDataError`)
and `fetch_and_validate(label, fetch, model)` (log → call → validate every
item against `model` → log count → return, catching/logging/re-raising on
failure). `ApiError` takes a `source` string (`"sports_io"`,
`"the_odds_api"`) baked into the exception message, so failures identify
which API broke without needing a subclass per client. `TIMEOUT_LIMIT`
and `RETRYABLE_STATUS` (`{500, 502, 503, 504}`) also live here, shared by
both clients; `@stamina.retry` wraps each client's own low-level fetch
method on `(ApiRateLimitError, ApiServerError,
requests.exceptions.RequestException)`.

The two APIs' error/rate-limit shapes are genuinely different, confirmed
against the live services rather than assumed from docs:

- **api-sports.io** wraps every response in an envelope
  (`get`/`parameters`/`errors`/`results`/`paging`/`response`) — `errors`
  can be populated on a 200, so `SportsIOClient._fetch_page` checks it
  even on success. It exposes *two* rate-limit headers
  (`x-ratelimit-remaining` per-minute, `x-ratelimit-requests-remaining`
  daily); `SportsIOClient._throttle_if_needed` reads the per-minute one
  and sleeps proactively before the next call instead of only reacting
  to a 429. None of its endpoints paginate today (confirmed live — a
  `page` param errors with "The Page field do not exist."), but
  `_request()`'s pagination loop still checks `paging` on every response
  so a future change wouldn't silently truncate to page 1.
- **The Odds API** has no envelope — `/v4/sports/{sport}/odds` returns a
  bare JSON array. Errors are real HTTP statuses with a small JSON body
  (`{"message", "error_code", "details_url"}`, confirmed via a real
  401/`INVALID_KEY`). It exposes one monthly usage-credit quota
  (`x-requests-remaining`) and a documented-but-unexposed 30-calls/second
  limit (429/`EXCEEDED_FREQ_LIMIT`) — there's no per-second-remaining
  header to throttle against proactively, so `TheOddsApiClient` only
  warns on low monthly credits and relies on `stamina`'s retry/backoff
  for 429s.

Each endpoint's URL path and the pydantic model that validates its items
are kept together on one `Endpoint` enum member in `sports_io_client.py`
(`Endpoint.TEAMS = ("/teams", Team)`, etc., via a custom `Endpoint.__init__`)
rather than in a separate lookup table, so the two can't drift out of
sync on an edit. The Odds API only has one endpoint, so it has no
equivalent enum — that pattern is specific to Sports IO's seven endpoints
(`LEAGUES`, `TEAMS`, `STANDINGS`, `GAMES`, `TEAM_STATISTICS`,
`GAME_EVENTS`, `ODDS`).

`SportsIOClient.get_standings()` drops a placeholder row that api-sports.io
mixes into an otherwise well-formed `/standings` response — `team.id: 816`,
`team.name: null`, `position: 0`, `conference`/`division: null`, every
stat zeroed — confirmed live, filtered on `team.name is not None` before
validation (the real per-team rows always have a name).

`get_current_season()`/`Endpoint.LEAGUES` (`/leagues?id=1&current=true`)
gives the current season's year and start/end dates. It's not fetched via
`_LEAGUE_SEASON_ENDPOINTS`'s automatic `league`/`season` params (those
don't apply here — `/leagues` takes `id`/`current` instead), so it's
called explicitly with its own params. Like Sports IO's own `Standing`
model, don't trust its `coverage.*` flags as behavioral signals — see the
CBS section above for the confirmed-false `coverage.standings` example
(same lesson, different source). Also see "Both Sports IO's and CBS's
'current season' checks fail closed" above — `get_current_season()`
returns `None` rather than a mismatched season if the API's current year
disagrees with `config.SEASON`.
