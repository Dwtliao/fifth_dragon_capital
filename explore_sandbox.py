"""
One-shot sandbox exploration script.
Runs OAuth dance, then dumps raw JSON for accounts, balances, portfolio, transactions, and orders.
Output is saved to sandbox_data/ so we can study the shapes before building the real sync.
"""
import json
import os
import webbrowser
from pathlib import Path
from dotenv import load_dotenv
from pyetrade.authorization import ETradeOAuth
from pyetrade.accounts import ETradeAccounts
from pyetrade.order import ETradeOrder

load_dotenv()

CONSUMER_KEY = os.environ["ETRADE_CONSUMER_KEY"]
CONSUMER_SECRET = os.environ["ETRADE_CONSUMER_SECRET"]
OUT_DIR = Path("sandbox_data")
OUT_DIR.mkdir(exist_ok=True)


def save(name, data):
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2))
    print(f"  saved -> {path}")


def auth():
    oauth = ETradeOAuth(CONSUMER_KEY, CONSUMER_SECRET)
    authorize_url = oauth.get_request_token()  # returns a URL string directly

    print(f"\nOpening browser for authorization...")
    webbrowser.open(authorize_url)
    print(f"URL (if browser didn't open): {authorize_url}\n")

    verifier = input("Paste the verifier code from the browser: ").strip()
    tokens = oauth.get_access_token(verifier)
    print("Auth successful.\n")
    return tokens["oauth_token"], tokens["oauth_token_secret"]


def explore(oauth_token, oauth_token_secret):
    accounts_client = ETradeAccounts(
        CONSUMER_KEY, CONSUMER_SECRET,
        oauth_token, oauth_token_secret,
        dev=True
    )
    orders_client = ETradeOrder(
        CONSUMER_KEY, CONSUMER_SECRET,
        oauth_token, oauth_token_secret,
        dev=True
    )

    # 1. Account list
    print("Fetching account list...")
    accounts_data = accounts_client.list_accounts(resp_format="json")
    save("accounts_list", accounts_data)

    accounts = (
        accounts_data.get("AccountListResponse", {})
        .get("Accounts", {})
        .get("Account", [])
    )
    if isinstance(accounts, dict):
        accounts = [accounts]

    print(f"Found {len(accounts)} account(s).\n")

    for acct in accounts:
        key = acct["accountIdKey"]
        acct_id = acct.get("accountId", key)
        print(f"--- Account: {acct_id} (key={key}) ---")

        # 2. Balance — call directly to avoid pyetrade sending realTimeNAV=True (capital T)
        print("  Fetching balance...")
        try:
            resp = accounts_client.session.get(
                f"https://apisb.etrade.com/v1/accounts/{key}/balance.json",
                params={"realTimeNAV": "true", "instType": acct["institutionType"]}
            )
            resp.raise_for_status()
            save(f"balance_{acct_id}", resp.json())
        except Exception as e:
            print(f"  Balance error: {e}")

        # 3. Portfolio / positions
        print("  Fetching portfolio (page 1)...")
        try:
            portfolio = accounts_client.get_account_portfolio(
                key, count=50, page_number=1, resp_format="json"
            )
            save(f"portfolio_{acct_id}", portfolio)
        except Exception as e:
            print(f"  Portfolio error: {e}")

        # 4. Transactions (last 30 days as a sample)
        print("  Fetching transactions...")
        try:
            from datetime import date, timedelta
            end = date.today()
            start = end - timedelta(days=365)
            txns = accounts_client.list_transactions(
                key, start_date=start, end_date=end,
                count=50, resp_format="json"
            )
            save(f"transactions_{acct_id}", txns)
        except Exception as e:
            print(f"  Transactions error: {e}")

        # 5. Orders — call directly to avoid pyetrade sending marketSession param
        print("  Fetching orders...")
        try:
            resp = accounts_client.session.get(
                f"https://apisb.etrade.com/v1/accounts/{key}/orders.json",
                params={"count": 100},
                headers={"consumerkey": CONSUMER_KEY}
            )
            if resp.status_code == 204:
                save(f"orders_{acct_id}", {"OrdersResponse": {"Order": []}})
            else:
                resp.raise_for_status()
                save(f"orders_{acct_id}", resp.json())
        except Exception as e:
            print(f"  Orders error: {e}")

        print()

    print(f"Done. All raw JSON saved to ./{OUT_DIR}/")
    print("Review the files to confirm field names before building the sync layer.")


if __name__ == "__main__":
    token, secret = auth()
    explore(token, secret)
