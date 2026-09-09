"""Client for CBS Data"""

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import stamina
from playwright.sync_api import APIRequestContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from api.cbs_models import (
    FootballPickemManagerPool,
    FootballPickemPoolHome,
    Member,
    PoolPeriodSummary,
    Team,
)
from config.config import (
    SEASON,
    STATE_PATH,
    CBSConfig,
    configure_logging,
    debugging_mode,
    get_cbs_config,
    get_players_path,
    get_pool_home_path,
    get_week_path,
)

logger = logging.getLogger(__name__)

PRODUCT_ID = 41406
CBS_BASE_URL = "https://picks.cbssports.com"
PICK_PATH = "/football/pickem/pools"

# CBS differs from Sports IO
ABBREV_CORRECTIONS = {
    "JAC": "JAX",  # Jacksonville
    "LAR": "LA",  # LA Rams
}

# data gets cached and resolved server side so I can't
# rely on graphql calls without manually finding their hashes.
# scraping the data instead:
#   (window[Symbol.for("ApolloSSRDataTransport")] ??= []).push({"rehydrate": {...}})
APOLLO_MARKER = '(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push('

# Status codes worth retrying:
RETRY_ERRORS = {429, 500, 502, 503, 504}
DEBUG_SLOW_MO_THROTTLE = 50  # used for debugging


class CBSDataError(RuntimeError):
    """generic cbs error handler"""


class FetchStatusError(RuntimeError):
    """server errors that likely will resolve without us
    other errors (2xx no data, 401,403/4, etc) should try
    logging in again instead
    """


@stamina.retry(on=(FetchStatusError, PlaywrightError))
def _fetch_html_data(context: APIRequestContext, weekly_url: str) -> str:
    """
    fetch results, retry network/server errors with backoff
    other errors return "" (blank data so it will retry logging in)
    """

    response = context.get(weekly_url)
    if response.status in RETRY_ERRORS:
        raise FetchStatusError(f"CBS returned {response.status} for {weekly_url}")
    elif response.status != 200:
        logger.warning(
            "Non-200 status, non-retryable status %s for %s",
            response.status,
            weekly_url,
        )
        return ""
    return response.text()


def _extract_common_pool(html: str, required_key: str) -> dict[str, Any] | None:
    """Find the SSR-embedded commonPool payload that has `required_key` —
    CBS embeds one Apollo push per query on a page, so the key tells us
    which one actually has the data we want."""
    for match in re.finditer(re.escape(APOLLO_MARKER), html):
        obj_start = html.find("{", match.end())
        try:
            payload, _ = json.JSONDecoder().raw_decode(html, obj_start)
        except json.JSONDecodeError:
            continue
        for entry in payload.get("rehydrate", {}).values():
            common_pool = entry.get("data", {}).get("commonPool")
            if common_pool and required_key in common_pool:
                return common_pool
    return None


class CBSClient:
    def __init__(self, cbs_config: CBSConfig):
        self.user = cbs_config.user
        self.password = cbs_config.password
        self.pool_id = cbs_config.pool_id
        self.state_path = STATE_PATH
        self.pool_url = f"{CBS_BASE_URL}{PICK_PATH}/{self.pool_id}"
        self.weekly_url = f"{self.pool_url}/standings/weekly"
        self.player_url = f"{self.pool_url}/players"
        self.login_url = f"https://www.cbssports.com/login?masterProductId={PRODUCT_ID}&product_abbrev=opm&show_opts=1&xurl={quote(self.weekly_url, '')}"
        self.pool_period_id: str | None = None

    def _credential_login(self, page: Page) -> None:
        logger.info("Credentials are stale, attempting to log in")

        try:
            page.goto(self.login_url)
            page.locator('input[name="email"]').fill(self.user)
            page.get_by_test_id("password").fill(self.password)
            page.get_by_test_id("submit-button").click()
            page.wait_for_url(f"{self.weekly_url}**")
        except Exception:
            logger.exception(
                "Login failed. CBS may have changed their login form, or credentials are wrong"
            )
            raise

        logger.info("Successfully logged in, updating credentials")

    def login(self) -> None:
        """Login via headless browser"""

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not debugging_mode(), slow_mo=DEBUG_SLOW_MO_THROTTLE
            )
            context = browser.new_context(
                storage_state=self.state_path if self.state_path.exists() else None
            )
            page = context.new_page()

            page.goto(self.weekly_url)

            if page.url != self.weekly_url.split("?", maxsplit=1)[0]:
                self._credential_login(page)

            context.storage_state(path=self.state_path)

    def _read_cbs_source_data(self, url: str, is_retry: bool = False) -> str:
        if self.state_path.exists():
            with sync_playwright() as p:
                api_request_context = p.request.new_context(
                    base_url=CBS_BASE_URL, storage_state=self.state_path
                )
                data = _fetch_html_data(api_request_context, url)
            if data:
                return data
        if is_retry:
            raise CBSDataError(f"Error reading CBS data from {url}")

        logger.info("Trouble reading data, falling back to log in and retry...")
        self.login()
        return self._read_cbs_source_data(url, True)

    def _fetch_common_pool(
        self, url: str, required_key: str, is_retry: bool = False
    ) -> dict[str, Any]:
        """Fetch `url` and pull out its commonPool payload, or raise."""
        html = self._read_cbs_source_data(url)
        if not html:
            raise CBSDataError(f"No HTML returned for {url}")

        data = _extract_common_pool(html, required_key)
        if data:
            return data

        if is_retry:
            raise CBSDataError(f"Could not find {required_key!r} data in {url}")

        logger.info(
            "%r not found in page, falling back to log in and retry...", required_key
        )
        self.login()
        return self._fetch_common_pool(url, required_key, True)

    def fetch_weekly_data(self) -> dict[str, Any]:
        """return weekly data (or currently week if no period specified)"""
        logger.info("Fetching weekly standings")
        weekly_url = (
            self.weekly_url
            if self.pool_period_id is None
            else f"{self.weekly_url}?poolPeriodId={self.pool_period_id}"
        )

        return self._fetch_common_pool(weekly_url, "poolPeriod")

    def fetch_user_data(self) -> dict[str, Any]:
        """return the pool's players/members data"""
        return self._fetch_common_pool(self.player_url, "members")

    def fetch_pool_home_data(self) -> dict[str, Any]:
        """return the pool-home page's data
        more per event details are found in this data
        (odds market, pick-ownership %, colors)"""
        url = (
            self.pool_url
            if self.pool_period_id is None
            else f"{self.pool_url}?poolPeriodId={self.pool_period_id}"
        )
        return self._fetch_common_pool(url, "season")


def write_data(data: dict[str, Any], file_path: Path) -> None:
    """Save data to data directory"""
    logger.info("Saving Data: %s", file_path)
    file_path.write_text(json.dumps(data), encoding="utf-8")


def _new_client() -> CBSClient:
    return CBSClient(get_cbs_config())


def get_cbs_users() -> list[Member]:
    """init cbs client and fetch/validate weekly data"""
    logger.info("Running CBS Player Fetch")
    try:
        cbs_client: CBSClient = _new_client()
        data = cbs_client.fetch_user_data()
        members: list[Member] = [Member.model_validate(m) for m in data["members"]]
        logger.info("Parsed %d pool members", len(members))
        write_data(data, get_players_path())
        return members
    except Exception:
        logger.exception("Player fetch failed")
        raise


def get_cbs_weekly(week: int = 0) -> FootballPickemManagerPool:
    """fetch and validate weekly data"""
    logger.info("Running CBS Pick Data Fetch")
    try:
        cbs_client: CBSClient = _new_client()
        data = cbs_client.fetch_weekly_data()
        cbs_data: FootballPickemManagerPool = FootballPickemManagerPool.model_validate(
            data
        )
        # TODO: map to pool summary for actual week details
        pool_period: PoolPeriodSummary = cbs_data.pool_period_for_week(week)
        week_int: int = pool_period.order

        logger.info(
            "Parsed pool %r: week %d | %d games | %d entries",
            cbs_data.name,
            week_int,
            len(cbs_data.pool_period.pool_events),
            cbs_data.ranked_entry_count,
        )
        write_data(data, get_week_path(week_int))
        return cbs_data
    except Exception:
        logger.exception("Scrape failed")
        raise


def get_cbs_pool_teams(week: int = 0) -> list[Team]:
    logger.info("Fetching Teams from user home page")
    try:
        cbs_data = get_cbs_pool_home(week)
        if cbs_data is None:
            return []
        pool_events = cbs_data.pool_period.pool_events
        return [
            team for game in pool_events for team in (game.away_team, game.home_team)
        ]
    except Exception:
        logger.exception("Fetching team data failed")
        raise


# TODO Wire up a weekly mapper (if needed)
def get_cbs_pool_home() -> FootballPickemPoolHome | None:
    """
    fetch and validate the pool-home page
    call get_cbs_weekly() for standings/picks.
    Returns None if CBS's season doesn't match config.SEASON (unsafe to keep going on).
    """
    logger.info("Running CBS Pool Home Fetch")
    try:
        cbs_client: CBSClient = _new_client()
        data = cbs_client.fetch_pool_home_data()
        cbs_data: FootballPickemPoolHome = FootballPickemPoolHome.model_validate(data)
        week_int: int = cbs_data.pool_period.order

        if cbs_data.season.year != SEASON:
            logger.warning(
                "CBS's season (%d) doesn't match config.SEASON (%d)! "
                "Update config SEASON and run again",
                cbs_data.season.year,
                SEASON,
            )
            return None

        logger.info(
            "Parsed pool %r: week %d | %d games",
            cbs_data.name,
            week_int,
            len(cbs_data.pool_period.pool_events),
        )
        write_data(data, get_pool_home_path(week_int))
        return cbs_data
    except Exception:
        logger.exception("Pool home fetch failed")
        raise


if __name__ == "__main__":
    configure_logging()
    get_cbs_weekly()
