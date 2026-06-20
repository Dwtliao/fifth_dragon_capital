import json
import time
from datetime import datetime, timezone, timedelta, date

from pyetrade.accounts import ETradeAccounts

from etrade_sync.auth import load_tokens
from etrade_sync.config import CONSUMER_KEY, CONSUMER_SECRET, DEV
from etrade_sync.db import get_connection
from etrade_sync.sync.accounts import _list_accounts
from etrade_sync.transaction_identity import (
    build_transaction_fingerprints,
    record_ingest_audit,
)

UPSERT_SQL = """
    INSERT INTO transactions
        (account_id_key, transaction_id, transaction_date, transaction_type,
         description, description2, amount, symbol, quantity, price, fee,
         settlement_date, source_system, source_record_key, source_payload_hash,
         canonical_fingerprint, dedupe_signature, raw)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (transaction_id) DO UPDATE SET
        transaction_type = EXCLUDED.transaction_type,
        description      = EXCLUDED.description,
        description2     = EXCLUDED.description2,
        amount           = EXCLUDED.amount,
        transaction_date = EXCLUDED.transaction_date,
        symbol           = COALESCE(EXCLUDED.symbol, transactions.symbol),
        quantity         = EXCLUDED.quantity,
        price            = EXCLUDED.price,
        fee              = EXCLUDED.fee,
        settlement_date  = EXCLUDED.settlement_date,
        source_system    = EXCLUDED.source_system,
        source_record_key = EXCLUDED.source_record_key,
        source_payload_hash = EXCLUDED.source_payload_hash,
        canonical_fingerprint = EXCLUDED.canonical_fingerprint,
        dedupe_signature = EXCLUDED.dedupe_signature,
        raw              = EXCLUDED.raw
"""

WATERMARK_UPSERT = """
    INSERT INTO sync_state (account_id_key, data_type, last_synced_at)
    VALUES (%s, 'transactions', NOW())
    ON CONFLICT (account_id_key, data_type) DO UPDATE SET
        last_synced_at = NOW()
"""

WATERMARK_SELECT = """
    SELECT last_synced_at FROM sync_state
    WHERE account_id_key = %s AND data_type = 'transactions'
"""


def _epoch_to_ts(epoch_val):
    """Convert epoch to datetime — handles both seconds and milliseconds."""
    if not epoch_val:
        return None
    secs = epoch_val / 1000 if epoch_val > 1e10 else epoch_val
    return datetime.fromtimestamp(secs, tz=timezone.utc)


def _get_start_date(cur, account_id_key) -> date:
    cur.execute(WATERMARK_SELECT, (account_id_key,))
    row = cur.fetchone()
    if row and row[0]:
        # Overlap by 1 day to avoid missing same-day transactions
        return (row[0] - timedelta(days=1)).date()
    return date.today() - timedelta(days=365 * 2)


def sync_transactions(account_filter=None, only=None,
                       start_date=None, end_date=None, from_beginning=False):
    """
    Sync transactions incrementally by default.
    Override date range with start_date/end_date or from_beginning=True.
    Returns dict with sync results for UI consumption.
    """
    if only is not None and only != "transactions":
        return {"synced": 0, "errors": []}

    token, secret = load_tokens()
    client = ETradeAccounts(CONSUMER_KEY, CONSUMER_SECRET, token, secret, dev=DEV)
    accounts = _list_accounts(client)
    if account_filter:
        accounts = [a for a in accounts if a["accountIdKey"] == account_filter]

    end = end_date or date.today()
    total = 0
    errors = []

    with get_connection() as conn:
        with conn.cursor() as cur:
            for acct in accounts:
                key = acct["accountIdKey"]

                if start_date:
                    start = start_date
                elif from_beginning:
                    start = date.today() - timedelta(days=365 * 2)
                else:
                    start = _get_start_date(cur, key)

                marker = None
                acct_count = 0
                acct_had_error = False

                while True:
                    try:
                        resp = client.list_transactions(
                            key,
                            start_date=start,
                            end_date=end,
                            count=50,
                            marker=marker,
                            resp_format="json",
                        )
                    except Exception as e:
                        errors.append(f"{key}: {e}")
                        acct_had_error = True
                        break

                    if not resp:
                        break

                    body = resp.get("TransactionListResponse", {})
                    txns = body.get("Transaction", [])
                    if isinstance(txns, dict):
                        txns = [txns]

                    for txn in txns:
                        brokerage = txn.get("brokerage", {})
                        product = brokerage.get("product", {})
                        symbol = (
                            product.get("symbol")
                            or brokerage.get("displaySymbol")
                            or None
                        )
                        if symbol:
                            symbol = symbol.strip() or None
                        identity = build_transaction_fingerprints(
                            account_id_key=key,
                            transaction_type=txn.get("transactionType"),
                            amount=txn.get("amount"),
                            symbol=symbol,
                            transaction_date=_epoch_to_ts(txn.get("transactionDate")),
                            settlement_date=_epoch_to_ts(brokerage.get("settlementDate")),
                            quantity=brokerage.get("quantity") or None,
                            price=brokerage.get("price") or None,
                            fee=brokerage.get("fee") or None,
                            description=txn.get("description"),
                            description2=txn.get("description2"),
                            source_payload=txn,
                            source_payload_drop_keys=["transactionId"],
                        )

                        source_record_key = str(txn["transactionId"])
                        cur.execute(UPSERT_SQL, (
                            key,
                            source_record_key,
                            _epoch_to_ts(txn.get("transactionDate")),
                            txn.get("transactionType"),
                            txn.get("description"),
                            txn.get("description2"),
                            txn.get("amount"),
                            symbol,
                            brokerage.get("quantity") or None,
                            brokerage.get("price") or None,
                            brokerage.get("fee") or None,
                            _epoch_to_ts(brokerage.get("settlementDate")),
                            "api",
                            source_record_key,
                            identity["source_payload_hash"],
                            identity["canonical_fingerprint"],
                            identity["dedupe_signature"],
                            json.dumps(txn),
                        ))
                        record_ingest_audit(
                            cur,
                            account_id_key=key,
                            transaction_id=source_record_key,
                            source_system="api",
                            source_record_key=source_record_key,
                            source_payload_hash=identity["source_payload_hash"],
                            canonical_fingerprint=identity["canonical_fingerprint"],
                            dedupe_signature=identity["dedupe_signature"],
                            write_status="upserted",
                            raw_payload=txn,
                        )
                        acct_count += 1

                    if not body.get("moreTransactions"):
                        break

                    marker = body.get("marker") or None
                    if not marker:
                        break
                    time.sleep(0.2)

                if not acct_had_error:
                    cur.execute(WATERMARK_UPSERT, (key,))
                total += acct_count

    print(f"  transactions: upserted {total} row(s)")
    return {"synced": total, "errors": errors}
