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
`get_cbs_weekly(pool_period_id=None)`, `get_cbs_users()`, and
`get_cbs_pool_home(pool_period_id=None)`. `__main__` calls
`configure_logging()` then `get_cbs_weekly()`; `get_cbs_users()`/
`get_cbs_pool_home()` have no CLI entry point yet — call them directly
(`from api.cbs_client import get_cbs_users, get_cbs_pool_home`).

**`pool_period_id` genuinely lets you fetch a past week, confirmed live
2026-09-15** — both the weekly-standings and pool-home pages accept
`?poolPeriodId=<id>` and return that specific period's data instead of
always the current one (found by inspecting a saved pool-home payload's
`poolPeriods` list, then testing the URL directly). Passing it sets
`CBSClient.pool_period_id` before the fetch (the client already had the
attribute and the URL-building logic for this - it just always stayed
`None`, so nothing before now ever actually exercised it). `weeks.cbs_pool_period_id`
already stores every period's id, not just the current one (`load_cbs_weeks()`
iterates the full `pool_periods` list on every run), so no extra fetching
is needed to discover a past week's id - `src/loaders/cbs_loader.py`'s
`backfill_cbs_week(week_number)` just reads it out of `weeks` and re-runs
the normal loaders against it. See `src/CLAUDE.md`'s Loaders section.

Deriving the returned week number differs by page, and this bit twice
while wiring the above up: the weekly-standings page's own `PoolPeriod`
(`cbs_data.pool_period`) has no `order` field at all - only
`PoolPeriodSummary` (the `pool_periods` list) and pool-home's richer
`PoolHomePoolPeriod` do. `get_cbs_weekly()` derives `week_int` by matching
`cbs_data.pool_period.id` against the summary list, rather than trusting
a caller-supplied week number the way the removed `pool_period_for_week(week)`
used to (that trusted the request, not the response - silently wrong if
the fetch didn't actually return what was asked for). `get_cbs_pool_home()`
can keep reading `.order` straight off `cbs_data.pool_period` since its
page's `PoolPeriod` really is the richer `PoolHomePoolPeriod` shape.

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

`pickInfo.cbsItemId` can also be `null` on an otherwise fully-revealed,
`displayStatus: "VISIBLE"` pick — confirmed live 2026-09-09 during this
season's actual Week 1 Thursday-night game. **This is not related to the
game being live/locked** (the tempting wrong theory this session initially
landed on, before checking): the pool only picks 5 of the week's ~16
games per user, but CBS still returns a `picks[]` entry (with `pickInfo`)
for every game per user, not just their five — `cbsItemId: null` simply
means this entry didn't pick that particular game. Confirmed by comparing
`cbsItemId` across all 38 entries for the same live game's `cbsSlotId`:
both populated and `null` values appeared regardless of `entry.isMine` or
the game's live status, with the split matching exactly which users had
picked that specific game. `cbs_loader.py`'s existing skip-with-a-warning
behavior on a `None` here is correct as-is — there's no pick to resolve a
team for, so nothing further needs building.

### CBS game status vocabulary — mostly guessed, partly confirmed

`game.game_status_desc`/`event.gameStatusDesc` isn't formally documented.
Confirmed live 2026-09-09 against a real in-progress game: `"SCHEDULED"`
and `"FINAL"` match what was originally guessed, but the in-progress value
is **`"INPROGRESS"` (no underscore)** — the original guess `"IN_PROGRESS"`
never matched, so `cbs_loader._cbs_status_to_common()` was silently
passing the raw value through unmapped instead of normalizing it (fixed).
`"HALFTIME"`/`"POSTPONED"`/`"CANCELLED"` are still unconfirmed guesses —
no live data has hit those states yet. If one of them turns out wrong the
symptom will be the same: a `"Unrecognized CBS game status"` warning in
the loader logs, not a crash.

`game.starts_at`/`startsAt` is epoch **milliseconds** — confirmed by its
field comment in `cbs_models.py` and cross-checked against a real kickoff
time. Sports IO's `game.date.timestamp` is epoch **seconds**, a different
unit for the same kind of value — `games.game_time` normalizes both to a
consistent ISO8601 UTC string (`"2026-09-14T17:00:00Z"`) before storage
specifically because of this mismatch; see `db/CLAUDE.md` for the stored
convention. Don't assume any raw timestamp from either source is
directly comparable to the other without converting first.

`PoolHomePoolEvent.markedFinalAt` was originally modeled as `str | None`
(guessed) — confirmed live 2026-09-09, the moment this season's first
game actually went final, that it's really epoch **milliseconds** like
`startsAt`, not a string; the wrong guess raised a pydantic
`ValidationError` that fully blocked `get_cbs_pool_home()` (and therefore
every downstream CBS loader) the instant a real game finished. Fixed to
`int | None` in `cbs_models.py`. This field isn't consumed by any loader
yet, so the fix was type-only — a reminder that an unused/never-read
field can still take the whole pipeline down if its type is wrong,
since pydantic validates every field on the payload whether or not
anything reads it afterward.

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

No team box-score endpoint anywhere (Sports IO's `games/statistics/teams`,
ESPN's `boxscore`, or CBS) exposes **punting stats** — checked all three
directly 2026-09-09, none have a punts/punt-yards/punt-average category.
The only punt-related data found anywhere is incidental play-by-play text
on ESPN's heavy `/core/nfl/game` endpoint (e.g. `"M.Dickson punts 42
yards..."`) when a punt happened to be the most recent play — not a real
stat, and that endpoint isn't used (see ESPN section below). Don't assume
this is just an unwired field; there's no clean source for it right now.

`get_live_games()` (`/games?live=all`) **cannot ever report a game going
FINAL** — confirmed live 2026-09-11: Sports IO's `live=all` filter is
server-side and simply stops returning a game the instant its status
leaves the live states, so a poller that only ever calls this endpoint
sees "still in progress" right up until the game vanishes from the
response, with no intermediate "now FINAL" sighting. `get_games_by_date(date)`
(`/games?date=YYYY-MM-DD`) was added for this reason — it returns every
game on that date regardless of status, so it's what
`sports_io_loader.py`'s live poll now uses instead (see `src/CLAUDE.md`'s
Orchestration section). `get_live_games()` itself is unused by any loader
now but kept since it's still a legitimate way to ask "what's live right
now" if a future use case wants exactly that.

## Pirate Weather Client

`api/weather_api.py` (Pirate Weather, a Dark Sky API-compatible service)
gets current conditions + a 7-day hourly forecast for a lat/lng — used for
pre-kickoff forecasts and (via `src/loaders/game_snapshots_loader.py`)
live in-game weather. Follows the same `fetch_and_validate`-style pattern
as Sports IO/The Odds API, but the single-object response (not a list)
needed a new `fetch_and_validate_one()` added to `api/api_helper.py`.

The API key is embedded directly in the URL **path** (Dark Sky-style, not
a header or query param) — every log/error message in `_fetch()`
deliberately uses the bare `API_URL` constant rather than the actual
request URL, to avoid leaking the key into logs. Confirmed live that a
401 error body doesn't echo the URL back either, so `response.text` is
still safe to include in `ApiDataError` messages.

Confirmed against the real OpenAPI spec (not just Dark Sky lore) two
request params that silently change what you get if omitted:
`extend=hourly` (without it, `hourly` is only the next 48h, not the full
7 days — would silently miss a forecast checked early in the week for a
Sunday kickoff), and `version=2` (unlocks `snowAccumulation`/
`iceAccumulation`/`liquidAccumulation` on hourly entries, more directly
game-relevant than `precipType`/`precipIntensity` alone). `units=us` is
pinned explicitly rather than relying on an undocumented default.

`alerts` is confirmed to come back as `[]` for a real location/time (no
active alert existed when built), but its shape (`title`/`severity`/
`time`/`expires`/`description`/`uri`/`regions`) is confirmed against the
real OpenAPI spec, not guessed from Dark Sky convention as originally
assumed — worth a live check once a real alert actually fires, same as
other "confirmed against real data" caveats in this file.

`snow_accumulation`/`ice_accumulation`/`liquid_accumulation` only ever
populate on `hourly.data[]` entries, never on `currently` — Pirate
Weather's `currently` block is a point-in-time reading and accumulation
is inherently period-based. `game_snapshots_loader.py` only reads
`currently`, so those three fields are always `None` there; capturing
them for live snapshots would mean also finding and reading the current
hour's `hourly` entry, deliberately deferred as added complexity for a
lower-value field.

## ESPN Client

`api/espn_client.py`/`api/espn_models.py` read ESPN's public NFL
scoreboard (`site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard`)
— the **only** source found for live field position (down/distance/yard
line/possession text/red zone/timeouts). This is a genuinely unofficial,
undocumented API: no public docs, no terms of service, no SLA. It's been
stable for years and is widely used by the sports-data community, but
treat it as a bonus/best-effort source, not a contract — every call site
that uses it is written to degrade gracefully (log and continue with
nulls) if it fails, same as CBS pool-home in
`game_snapshots_loader.py`.

Confirmed live 2026-09-09 by checking a full week's scoreboard:
`situation` (the field-position data) and `odds` are **mutually
exclusive** on this endpoint — `situation` appears only once a
competition's status is genuinely `IN_PROGRESS` (absent on all scheduled
games), `odds` appears only pre-game and disappears the moment a game
goes live. Deliberately not modeling `odds` from this endpoint at all —
The Odds API already covers pre-game odds, and tracking *live* in-game
odds would require ESPN's much heavier `/core/nfl/game?xhr=1` endpoint
(400KB+ per game, its `pickcenter` section) instead of this lightweight
one — scoped out as a deliberate v1 decision, not an oversight.

`ABBREV_CORRECTIONS` (in `espn_client.py`, same pattern as
`cbs_client.py`'s) maps the 2 confirmed cases where ESPN's team
abbreviation differs from `teams.abbreviation` (Sports IO's convention):
`LAR`→`LA` (Rams), `WSH`→`WAS` (Washington) — confirmed live by diffing
the full 32-team abbreviation sets, same method used for CBS's
corrections. `game_snapshots_loader.py`'s
`VENUE_NAME_CORRECTIONS`-equivalent for stadium names lives in
`sports_io_loader.py` instead (`"Reliant Stadium"`→`"NRG Stadium"`,
`"FC Bayern Munich Stadium"`→`"Allianz Arena"`) — Sports IO's `venue.name`
sometimes uses stale or sponsorship-neutral names instead of the venue's
real/current one; both corrections found by cross-checking real 2026
schedule data, not guessed.

ESPN events are matched onto `games` by `(home_abbrev, away_abbrev)` on
first sighting (there's no other shared id up front), then
`games.espn_event_id` gets backfilled so later runs join directly instead
of re-matching by name every time — same "match once, then join by id"
pattern as `odds_loader.py`'s `odds_api_event_id`.

`get_scoreboard(week)` (added 2026-09-26) fetches a specific
regular-season week of `config.SEASON` via
`?seasontype=2&week=N&dates={SEASON}`; with no week it's the current
week, as before. Confirmed live that a single date-range query for the
whole season (`?dates=YYYYMMDD-YYYYMMDD`) returns a 400, so a full-season
sync is one call per week. `competitions[].neutralSite` is what
`src/loaders/espn_loader.py` persists to `games.neutral_site`.
