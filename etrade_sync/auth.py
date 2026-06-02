import json
import webbrowser

from pyetrade.authorization import ETradeOAuth

from etrade_sync.config import CONSUMER_KEY, CONSUMER_SECRET, TOKEN_FILE


def run_auth_flow():
    """Complete OAuth dance, save tokens to TOKEN_FILE."""
    oauth = ETradeOAuth(CONSUMER_KEY, CONSUMER_SECRET)
    authorize_url = oauth.get_request_token()

    print(f"Opening browser for E*TRADE authorization...")
    webbrowser.open(authorize_url)
    print(f"If browser didn't open: {authorize_url}\n")

    verifier = input("Paste the verifier code from the browser: ").strip()
    tokens = oauth.get_access_token(verifier)

    _save_tokens(tokens)
    print(f"Tokens saved to {TOKEN_FILE}")
    return tokens["oauth_token"], tokens["oauth_token_secret"]


def load_tokens():
    """Load saved tokens from TOKEN_FILE. Returns (oauth_token, oauth_token_secret).

    Tokens expire at midnight ET. If an API call returns 401, run `python -m etrade_sync auth`.
    """
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"No token file found at {TOKEN_FILE}. Run `python -m etrade_sync auth` first."
        )

    data = json.loads(TOKEN_FILE.read_text())
    return data["oauth_token"], data["oauth_token_secret"]


def _save_tokens(tokens: dict):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.chmod(0o600) if TOKEN_FILE.exists() else None
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    TOKEN_FILE.chmod(0o600)
