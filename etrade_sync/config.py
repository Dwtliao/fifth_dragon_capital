import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


CONSUMER_KEY = _require("ETRADE_CONSUMER_KEY")
CONSUMER_SECRET = _require("ETRADE_CONSUMER_SECRET")
DEV = os.environ.get("ETRADE_DEV", "true").lower() == "true"
TOKEN_FILE = Path(os.environ.get("ETRADE_TOKEN_FILE", "~/.config/etrade/tokens.json")).expanduser()
DATABASE_URL = _require("DATABASE_URL")
