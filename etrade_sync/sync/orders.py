import json
import time
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from etrade_sync.auth import load_tokens
from etrade_sync.config import CONSUMER_KEY, CONSUMER_SECRET, DEV
from etrade_sync.db import get_connection
from etrade_sync.sync.accounts import _list_accounts
from pyetrade.accounts import ETradeAccounts

BASE_URL = f"https://{'apisb' if DEV else 'api'}.etrade.com"

ORDER_UPSERT = """
    INSERT INTO orders
        (account_id_key, order_id, order_type, total_order_value,
         total_commission, placed_time, status, raw)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (order_id) DO UPDATE SET
        status            = EXCLUDED.status,
        total_order_value = EXCLUDED.total_order_value,
        total_commission  = EXCLUDED.total_commission,
        raw               = EXCLUDED.raw
"""

DETAIL_INSERT = """
    INSERT INTO order_details
        (order_id, order_number, symbol, symbol_desc, security_type,
         order_action, price_type, order_term, limit_price, stop_price,
         status, placed_time, executed_time, ordered_quantity, filled_quantity,
         avg_execution_price, estimated_commission, call_put,
         expiry_year, expiry_month, expiry_day, strike_price, raw)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

WATERMARK_UPSERT = """
    INSERT INTO sync_state (account_id_key, data_type, last_synced_at)
    VALUES (%s, 'orders', NOW())
    ON CONFLICT (account_id_key, data_type) DO UPDATE SET
        last_synced_at = NOW()
"""

WATERMARK_SELECT = """
    SELECT last_synced_at FROM sync_state
    WHERE account_id_key = %s AND data_type = 'orders'
"""


def _epoch_to_ts(value):
    """Convert epoch to datetime — handles both seconds and milliseconds."""
    if not value:
        return None
    # Values > 1e10 are milliseconds (current time in ms is ~1.7e12)
    secs = value / 1000 if value > 1e10 else value
    return datetime.fromtimestamp(secs, tz=timezone.utc)


def _first_detail_status(order):
    details = order.get("OrderDetail", [])
    if details:
        return details[0].get("status")
    return None


def _first_placed_time(order):
    details = order.get("OrderDetail", [])
    if details:
        return _epoch_to_ts(details[0].get("placedTime"))
    return None


def sync_orders(account_filter=None, only=None,
                start_date=None, end_date=None, from_beginning=False):
    """
    Sync orders incrementally by default.
    Override date range with start_date/end_date or from_beginning=True.
    Returns dict with sync results for UI consumption.
    """
    if only is not None and only != "orders":
        return {"synced": 0, "errors": []}

    token, secret = load_tokens()
    client = ETradeAccounts(CONSUMER_KEY, CONSUMER_SECRET, token, secret, dev=DEV)
    accounts = _list_accounts(client)
    if account_filter:
        accounts = [a for a in accounts if a["accountIdKey"] == account_filter]

    end = end_date or date.today()
    total_orders = 0
    total_legs = 0
    errors = []

    with get_connection() as conn:
        with conn.cursor() as cur:
            for acct in accounts:
                key = acct["accountIdKey"]
                marker = None
                acct_orders = 0
                acct_legs = 0
                acct_had_error = False

                # Build date range for this account
                if start_date:
                    acct_start = start_date
                elif from_beginning:
                    acct_start = date.today() - timedelta(days=365 * 2)
                else:
                    cur.execute(WATERMARK_SELECT, (key,))
                    row = cur.fetchone()
                    acct_start = (row[0] - timedelta(days=1)).date() if row and row[0] else date.today() - timedelta(days=365 * 2)

                while True:
                    params = {"count": 100}
                    if marker:
                        params["marker"] = marker
                    if acct_start:
                        params["fromDate"] = acct_start.strftime("%m%d%Y")
                        params["toDate"] = end.strftime("%m%d%Y")

                    try:
                        resp = client.session.get(
                            f"{BASE_URL}/v1/accounts/{key}/orders.json",
                            params=params,
                            headers={"consumerkey": CONSUMER_KEY},
                            timeout=(5, 15),  # (connect, read) timeouts
                        )
                        if resp.status_code == 204:
                            break
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:
                        errors.append(f"{key}: {e}")
                        print(f"  orders: skipping {key} — {e}")
                        acct_had_error = True
                        break

                    orders = data.get("OrdersResponse", {}).get("Order", [])
                    if isinstance(orders, dict):
                        orders = [orders]
                    if not orders:
                        break

                    for order in orders:
                        order_id = order["orderId"]

                        # Upsert order header
                        cur.execute(ORDER_UPSERT, (
                            key,
                            order_id,
                            order.get("orderType"),
                            order.get("totalOrderValue"),
                            order.get("totalCommission"),
                            _first_placed_time(order),
                            _first_detail_status(order),
                            json.dumps(order),
                        ))

                        # Replace detail legs — status/fills change so delete+reinsert
                        cur.execute("DELETE FROM order_details WHERE order_id = %s", (order_id,))

                        for detail in order.get("OrderDetail", []):
                            instruments = detail.get("Instrument", [])
                            if isinstance(instruments, dict):
                                instruments = [instruments]

                            for instrument in instruments:
                                product = instrument.get("Product", {})
                                cur.execute(DETAIL_INSERT, (
                                    order_id,
                                    detail.get("orderNumber"),
                                    product.get("symbol"),
                                    instrument.get("symbolDescription"),
                                    product.get("securityType"),
                                    instrument.get("orderAction"),
                                    detail.get("priceType"),
                                    detail.get("orderTerm"),
                                    detail.get("limitPrice"),
                                    detail.get("stopPrice"),
                                    detail.get("status"),
                                    _epoch_to_ts(detail.get("placedTime")),
                                    _epoch_to_ts(detail.get("executedTime")),
                                    instrument.get("orderedQuantity"),
                                    instrument.get("filledQuantity"),
                                    instrument.get("averageExecutionPrice"),
                                    instrument.get("estimatedCommission"),
                                    product.get("callPut"),
                                    product.get("expiryYear") or None,
                                    product.get("expiryMonth") or None,
                                    product.get("expiryDay") or None,
                                    product.get("strikePrice") or None,
                                    json.dumps(instrument),
                                ))
                                acct_legs += 1

                        acct_orders += 1

                    next_marker = data.get("OrdersResponse", {}).get("marker")
                    if not next_marker:
                        break
                    marker = next_marker
                    time.sleep(0.2)

                if not acct_had_error:
                    cur.execute(WATERMARK_UPSERT, (key,))
                total_orders += acct_orders
                total_legs += acct_legs

    print(f"  orders: upserted {total_orders} header(s), {total_legs} detail leg(s)")
    return {"synced_orders": total_orders, "synced_legs": total_legs, "errors": errors}
