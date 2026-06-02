import json
import time

from pyetrade.accounts import ETradeAccounts

from etrade_sync.auth import load_tokens
from etrade_sync.config import CONSUMER_KEY, CONSUMER_SECRET, DEV
from etrade_sync.db import get_connection
from etrade_sync.sync.accounts import _list_accounts

INSERT_SQL = """
    INSERT INTO positions
        (account_id_key, position_id, symbol, symbol_desc, security_type,
         position_type, quantity, cost_per_share, total_cost, market_value,
         total_gain, total_gain_pct, days_gain, days_gain_pct, pct_of_portfolio, raw)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def sync_positions(account_filter=None, only=None):
    if only is not None and only != "positions":
        return

    token, secret = load_tokens()
    client = ETradeAccounts(CONSUMER_KEY, CONSUMER_SECRET, token, secret, dev=DEV)
    accounts = _list_accounts(client)
    if account_filter:
        accounts = [a for a in accounts if a["accountIdKey"] == account_filter]

    total = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for acct in accounts:
                key = acct["accountIdKey"]
                page = 1
                while True:
                    try:
                        resp = client.get_account_portfolio(
                            key, count=50, page_number=page, resp_format="json"
                        )
                    except Exception as e:
                        print(f"  positions: skipping {key} page {page} — {e}")
                        break

                    portfolios = (
                        resp.get("PortfolioResponse", {})
                        .get("AccountPortfolio", [])
                    )
                    if not portfolios:
                        break

                    positions = portfolios[0].get("Position", [])
                    if isinstance(positions, dict):
                        positions = [positions]

                    for pos in positions:
                        product = pos.get("Product", {})
                        cur.execute(INSERT_SQL, (
                            key,
                            pos.get("positionId"),
                            product.get("symbol"),
                            pos.get("symbolDescription"),
                            product.get("securityType"),
                            pos.get("positionType"),
                            pos.get("quantity"),
                            pos.get("costPerShare"),
                            pos.get("totalCost"),
                            pos.get("marketValue"),
                            pos.get("totalGain"),
                            pos.get("totalGainPct"),
                            pos.get("daysGain"),
                            pos.get("daysGainPct"),
                            pos.get("pctOfPortfolio"),
                            json.dumps(pos),
                        ))
                        total += 1

                    next_page = portfolios[0].get("nextPageNo")
                    if not next_page:
                        break
                    page = int(next_page)
                    time.sleep(0.2)

    print(f"  positions: inserted {total} row(s)")
