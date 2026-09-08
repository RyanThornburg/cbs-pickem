# api/CLAUDE.md

## CBS Scraping

`api/cbs_client.py`'s `CBSClient` class holds all CBS-pool-specific state — credentials and the weekly/player/login URLs derived from `CBSConfig` — resolved once in
`__init__` rather than as module-level globals computed at import time.
Its public surface is `login()`, `fetch_weekly_data()`, and
`fetch_user_data()`; the private `_fetch_common_pool(url, required_key)`
does the actual fetch-and-extract-or-raise, shared by both.

Two module-level functions build a `CBSClient` and drive it end-to-end
(fetch, validate against a pydantic model, log, write to disk):
`get_cbs_weekly(week=0)` and `get_cbs_users()`. `__main__` calls
`configure_logging()` then `get_cbs_weekly()`; `get_cbs_users()` has no CLI
entry point yet — call it directly (`from api.cbs_client import
get_cbs_users`).

Transient fetch failures (network errors, 5xx/429) are retried with
backoff via `stamina` in `_fetch_html_data()` — but login is deliberately
*not* auto-retried, since repeated failed logins risk tripping CBS's
anti-bot/lockout defenses, so `_credential_login()` fails fast and logs
instead.

Both pages CBS serves (weekly standings, pool players) embed the same SSR
Apollo transport shape; `_extract_common_pool(html, required_key)` finds
the payload that actually has `required_key` (`"poolPeriod"` for weekly,
`"members"` for players), since CBS pushes one Apollo entry per query on a
page and `required_key` is how the code tells them apart. `api/cbs_models.py`'s
module docstring documents the actual CBS payload shape these models parse
(a denormalized `commonPool` response, not the `__APOLLO_STATE__`
normalized cache its typenames might suggest) — including that CBS reuses
the same GraphQL `__typename` for different shapes depending on which
page/query returned it (e.g. `Member.email` is only ever populated when the
payload came from the players page).

Each scrape's raw JSON is saved under `config.config.DATA_DIR`
(`data/<season>/`, season from `config.config.SEASON`): weekly standings at
`data/<season>/Week<NN>/cbs_week_<NN>.json`, pool members at
`data/<season>/players.json`.

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
equivalent enum — that pattern is specific to Sports IO's six endpoints.
