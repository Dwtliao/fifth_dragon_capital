import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Always load the repo-local .env and let it override inherited launchd/shell
# variables so the job behavior matches the checked-in environment file.
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


CONSUMER_KEY = _require("ETRADE_CONSUMER_KEY")
CONSUMER_SECRET = _require("ETRADE_CONSUMER_SECRET")
DEV         = os.environ.get("ETRADE_DEV",         "false").lower() == "true"
LIVE_ORDERS = os.environ.get("ETRADE_LIVE_ORDERS", "false").lower() == "true"
TOKEN_FILE = Path(os.environ.get("ETRADE_TOKEN_FILE", "~/.config/etrade/tokens.json")).expanduser()
DATABASE_URL = _require("DATABASE_URL")
