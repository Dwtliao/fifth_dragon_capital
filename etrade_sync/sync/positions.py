import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from pyetrade.accounts import ETradeAccounts

from etrade_sync.auth import load_tokens
from etrade_sync.config import CONSUMER_KEY, CONSUMER_SECRET, DEV
from etrade_sync.db import get_connection
from etrade_sync.sync.accounts import _list_accounts

INSERT_SQL = """
    INSERT INTO positions
        (account_id_key, fetched_at, position_id, symbol, symbol_desc, security_type,
         position_type, quantity, cost_per_share, total_cost, market_value,
         total_gain, total_gain_pct, days_gain, days_gain_pct, pct_of_portfolio, raw)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

BROKER_LOT_INSERT_SQL = """
    INSERT INTO broker_position_lots
        (account_id_key, symbol, position_id, position_lot_id, acquired_date,
         original_quantity, remaining_quantity, price, total_cost, market_value,
         total_gain, position_fetched_at, raw)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (account_id_key, symbol, position_lot_id, position_fetched_at)
    DO NOTHING
"""


def _epoch_to_date(epoch_ms):
    """Convert E*TRADE's millisecond epoch acquisition date to a UTC date."""
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc).date()


def extract_broker_lots(account_id_key, symbol, position_id, response, position_fetched_at):
    """Normalize one E*TRADE PositionLotsResponse into insert-ready rows.

    E*TRADE's position snapshot supplies a ``lotsDetails`` URL per equity. The
    linked endpoint reports remaining quantities after any specifically selected
    lot sales, so these rows must remain independent from local FIFO open_lots.
    """
    lots = response.get("PositionLotsResponse", {}).get("PositionLot", [])
    if isinstance(lots, dict):
        lots = [lots]

    rows = []
    for lot in lots:
        lot_id = lot.get("positionLotId")
        remaining = lot.get("remainingQty")
        if lot_id is None or remaining is None:
            continue
        try:
            if Decimal(str(remaining)) <= 0:
                continue
        except InvalidOperation:
            continue
        rows.append((
            account_id_key,
            symbol,
            position_id,
            lot_id,
            _epoch_to_date(lot.get("acquiredDate")),
            lot.get("originalQty"),
            remaining,
            lot.get("price"),
            lot.get("totalCost"),
            lot.get("marketValue"),
            lot.get("totalGain"),
            position_fetched_at,
            json.dumps(lot),
        ))
    return rows


def _fetch_broker_lot_response(client, pos):
    """Fetch E*TRADE's lot-detail URL embedded in a position response."""
    url = pos.get("lotsDetails")
    if not url:
        raise RuntimeError("E*TRADE position response did not include lotsDetails")
    response = client.session.get(url, timeout=15)
    response.raise_for_status()
    return response.json()


def sync_positions(account_filter=None, only=None):
    if only is not None and only != "positions":
        return

    token, secret = load_tokens()
    client = ETradeAccounts(CONSUMER_KEY, CONSUMER_SECRET, token, secret, dev=DEV)
    accounts = _list_accounts(client)
    if account_filter:
        accounts = [a for a in accounts if a["accountIdKey"] == account_filter]

    total = 0
    broker_lot_total = 0
    lot_warnings = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            for acct in accounts:
                key = acct["accountIdKey"]
                position_fetched_at = datetime.now(timezone.utc)
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
                        symbol = product.get("symbol")
                        security_type = product.get("securityType")
                        cur.execute(INSERT_SQL, (
                            key,
                            position_fetched_at,
                            pos.get("positionId"),
                            symbol,
                            pos.get("symbolDescription"),
                            security_type,
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

                        # Lots are authoritative only for equities and are stored
                        # separately from FIFO-derived open_lots.
                        if security_type == "EQ" and symbol:
                            try:
                                lot_response = _fetch_broker_lot_response(client, pos)
                                lot_rows = extract_broker_lots(
                                    key, symbol, pos.get("positionId"), lot_response,
                                    position_fetched_at,
                                )
                                if lot_rows:
                                    cur.executemany(BROKER_LOT_INSERT_SQL, lot_rows)
                                    broker_lot_total += len(lot_rows)
                            except Exception as e:
                                message = f"{key} {symbol}: {e}"
                                print(f"  broker lots: WARNING — skipping {message}")
                                lot_warnings.append(message)

                    next_page = portfolios[0].get("nextPageNo")
                    if not next_page:
                        break
                    page = int(next_page)
                    time.sleep(0.2)

    print(f"  positions: inserted {total} row(s)")
    print(f"  broker lots: inserted {broker_lot_total} row(s)")
    if lot_warnings:
        print(
            f"  broker lots: WARNING — {len(lot_warnings)} position(s) could not be fetched; "
            "aggregate positions were synced successfully"
        )
        # Do not return the framework's reserved ``errors`` key: a broker lot
        # endpoint failure should not fail an otherwise valid positions sync.
        return {"broker_lot_warnings": lot_warnings}
