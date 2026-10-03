# config/config.py
import logging
import logging.handlers
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

SEASON = 2026

# Every pick'em entry makes this many picks a week
PICKS_PER_WEEK = 5


@dataclass(frozen=True)
class Period:
    """One standings period the pool pays out on. end_week None runs
    through the season's last week."""

    key: str
    label: str
    start_week: int
    end_week: int | None
    paid_places: int
    # last place among users who made PICKS_PER_WEEK picks every week of the period
    pay_last_place: bool = False

    def covers(self, week_number: int) -> bool:
        return week_number >= self.start_week and (
            self.end_week is None or week_number <= self.end_week
        )


# The pool's payout structure for SEASON - "overall" is the season-long
# standings and has to stay. Change it at the season bump, before
# new_season.py, never mid-season (season_close_out.py saves whatever is
# here as the closed season's structure).
PERIODS = (
    Period("overall", "Overall", 1, None, paid_places=5),
    Period("first_half", "First Half", 1, 9, paid_places=3),
    Period("second_half", "Second Half", 10, None, paid_places=3),
)
PERIODS_BY_KEY = {period.key: period for period in PERIODS}

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / str(SEASON)
STATE_PATH: Path = PROJECT_ROOT / "secrets/state.json"
SCHEMA_PATH: Path = PROJECT_ROOT / "db/schema.sql"
# src/orchestration.py's one-tick-at-a-time lock files
LOCK_DIR = PROJECT_ROOT / "locks"

# logging config
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "log.log"
ERROR_LOG_FILE = LOG_DIR / "error.log"
LOG_FILES_TO_KEEP = 5
LOG_MAX_BYTES = 1_000_000


@dataclass
class CBSConfig:
    user: str
    password: str
    pool_id: str


def get_players_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "players.json"


def _week_file(week: int, prefix: str) -> Path:
    """data/Week{NN}/{prefix}_{NN}.json, creating the week's folder"""
    path_week = DATA_DIR / f"Week{week:02d}"
    path_week.mkdir(parents=True, exist_ok=True)
    return path_week / f"{prefix}_{week:02d}.json"


def get_week_path(week: int) -> Path:
    return _week_file(week, "cbs_week")


def get_pool_home_path(week: int) -> Path:
    return _week_file(week, "cbs_pool_home")


def configure_logging(level: int = logging.INFO):
    """logging setup"""

    LOG_DIR.mkdir(exist_ok=True)

    rotate_handler = logging.handlers.RotatingFileHandler(
        filename=LOG_FILE, backupCount=LOG_FILES_TO_KEEP - 1, maxBytes=LOG_MAX_BYTES
    )

    error_handler = logging.handlers.RotatingFileHandler(
        filename=ERROR_LOG_FILE,
        backupCount=LOG_FILES_TO_KEEP - 1,
        maxBytes=LOG_MAX_BYTES,
    )
    error_handler.setLevel(logging.ERROR)

    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(), rotate_handler, error_handler],
        force=True,
    )


def load_env(env: str = "local") -> bool:
    """Load env specific configuration"""

    env_file = Path(__file__).parent / f".env.{env}"
    if env_file.exists():
        load_dotenv(env_file)
        logger.debug("Loaded %s environment", env)
    else:
        logger.error("Environment file not found")
        return False

    # validate required fields
    required_vars = [
        "CF_ACCOUNT_ID",
        "CF_D1_TOKEN",
        "CF_D1_DATABASE_ID",
        "CBS_USER",
        "CBS_PASS",
        "CF_KV_NAMESPACE_ID",
        "CF_KV_TOKEN",
        "SPORTS_IO_API_KEY",
    ]
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        logger.error(
            "Missing required environment variables %s", ", ".join(missing_vars)
        )
        return False

    return True


def cli_env() -> str:
    """the [local|prod] command-line argument - local if none given"""
    return sys.argv[1] if len(sys.argv) > 1 else "local"


def run_cli(main: Callable[[], object]) -> None:
    """a module's `if __name__ == "__main__":` - configure logging, load the
    env named on the command line (see cli_env()), then run `main`. Any
    further arguments (e.g. a week number) are main()'s to read."""
    configure_logging()
    if not load_env(cli_env()):
        sys.exit(1)
    main()


def get_d1_config() -> dict[str, str]:
    """Returns Cloudflare D1 connection config"""
    return {
        "account_id": os.getenv("CF_ACCOUNT_ID", ""),
        "database_id": os.getenv("CF_D1_DATABASE_ID", ""),
        "api_token": os.getenv("CF_D1_TOKEN", ""),
    }


def get_kv_config() -> dict[str, str]:
    "return cloudflare kv config"
    return {
        "account_id": os.getenv("CF_ACCOUNT_ID", ""),
        "kv_namespace_id": os.getenv("CF_KV_NAMESPACE_ID", ""),
        "api_token": os.getenv("CF_KV_TOKEN", ""),
    }


# CBS/Sports IO/The Odds API/Pirate Weather credentials are identical in .env.local and .env.prod
# no need to pass in env/just keep value same in both .env files
def get_cbs_config() -> CBSConfig:
    """Returns CBS Sports login credentials and pool id"""
    if not load_env():
        raise RuntimeError("CBS config missing/invalid")

    return CBSConfig(
        user=os.getenv("CBS_USER", ""),
        password=os.getenv("CBS_PASS", ""),
        pool_id=os.getenv("CBS_POOL_ID", ""),
    )


def get_sports_io_api() -> str:
    """load sports io api from config"""
    if not load_env():
        raise RuntimeError("Sports IO API config missing/invalid")
    return os.getenv("SPORTS_IO_API_KEY", "")


def get_the_odds_api() -> str:
    """load the odds api key from config"""
    if not load_env():
        raise RuntimeError("The Odds API config missing/invalid")
    return os.getenv("THE_ODDS_API_KEY", "")


def get_weather_api() -> str:
    """load the weather api key from config"""
    if not load_env():
        raise RuntimeError("Weather API config missing/invalid")
    return os.getenv("WEATHER_API_KEY", "")


def debugging_mode() -> bool:
    """set to debug or not"""
    return os.getenv("DEBUG", "").lower() == "true"
