# config/config.py
import logging
import logging.handlers
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

SEASON = 2026

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / str(SEASON)
STATE_PATH: Path = PROJECT_ROOT / "secrets/state.json"
SCHEMA_PATH: Path = PROJECT_ROOT / "db/schema.sql"

# logging config
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "log.log"
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


def get_week_path(week: int) -> Path:
    week_str = f"{week:02d}"
    path_week = DATA_DIR / f"Week{week_str}"
    path_week.mkdir(parents=True, exist_ok=True)
    return path_week / f"cbs_week_{week_str}.json"


def get_pool_home_path(week: int) -> Path:
    week_str = f"{week:02d}"
    path_week = DATA_DIR / f"Week{week_str}"
    path_week.mkdir(parents=True, exist_ok=True)
    return path_week / f"cbs_pool_home_{week_str}.json"


def configure_logging(level: int = logging.INFO):
    """logging setup"""

    LOG_DIR.mkdir(exist_ok=True)

    rotate_handler = logging.handlers.RotatingFileHandler(
        filename=LOG_FILE, backupCount=LOG_FILES_TO_KEEP - 1, maxBytes=LOG_MAX_BYTES
    )

    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(), rotate_handler],
        force=True,
    )


def load_env(env: str = "local") -> bool:
    """Load env specific configuration"""

    env_file = Path(__file__).parent / f".env.{env}"
    if os.path.exists(env_file):
        load_dotenv(env_file)
        logger.debug("Loaded %s environment", env)
    else:
        logger.error("Environment file not found")
        return False

    # validate required fields
    required_vars = [
        "CF_ACCOUNT_ID",
        "CF_API_TOKEN",
        "CF_D1_DATABASE_ID",
        "CBS_USER",
        "CBS_PASS",
    ]
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        logger.error(
            "Missing required environment variables %s", (",").join(missing_vars)
        )
        return False

    return True


def get_d1_config() -> dict[str, str]:
    """Returns Cloudflare D1 connection config"""
    return {
        "account_id": os.getenv("CF_ACCOUNT_ID", ""),
        "database_id": os.getenv("CF_D1_DATABASE_ID", ""),
        "api_token": os.getenv("CF_API_TOKEN", ""),
    }


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


def debugging_mode() -> bool:
    """set to debug or not"""
    return os.getenv("DEBUG", "").lower() == "true"
