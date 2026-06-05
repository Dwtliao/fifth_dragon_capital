import json

from etrade_sync.db import get_connection
from etrade_sync.maintenance import backfill_transaction_provenance

# Maps E*TRADE transaction_type → normalized ledger event_type.
# None means the type is determined at runtime from the amount sign.
_EVENT_TYPE_MAP = {
    "Bought":             "buy",
    "Sold":               "sell",
    "Dividend":           "dividend",
    "Qualified Dividend": "dividend_qualified",
    "Interest Income":    "interest",
    "Fee":                "fee",
    "Transfer":           "transfer",
    "Online Transfer":    None,   # deposit or withdrawal based on amount sign
    "POS":                "withdrawal",
    "Bill Payment":       "withdrawal",
    "Stock Split":        "split",
    "Redemption":         "redemption",
}


def _map_event_type(transaction_type, amount):
    mapped = _EVENT_TYPE_MAP.get(transaction_type)
    if mapped is None:
        return "deposit" if (amount or 0) >= 0 else "withdrawal"
    return mapped


_UPSERT = """
    INSERT INTO ledger
        (account_id_key, event_timestamp, settlement_date, event_type, symbol,
         quantity, price, gross_amount, net_amount, fee,
         source_table, source_id, source_line_id, raw)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'transactions', %s, '0', %s)
    ON CONFLICT (source_table, source_id, source_line_id) DO UPDATE SET
        event_type      = EXCLUDED.event_type,
        symbol          = COALESCE(EXCLUDED.symbol, ledger.symbol),
        quantity        = EXCLUDED.quantity,
        price           = EXCLUDED.price,
        gross_amount    = EXCLUDED.gross_amount,
        net_amount      = EXCLUDED.net_amount,
        fee             = EXCLUDED.fee
"""

_CANONICAL_TRANSACTIONS_SQL = """
    WITH ranked AS (
        SELECT
            t.account_id_key,
            t.transaction_id,
            t.transaction_date,
            t.settlement_date,
            t.transaction_type,
            t.symbol,
            t.quantity,
            t.price,
            t.amount,
            t.fee,
            t.raw,
            ROW_NUMBER() OVER (
                PARTITION BY t.account_id_key, COALESCE(t.dedupe_signature, t.canonical_fingerprint, t.transaction_id)
                ORDER BY
                    CASE COALESCE(t.source_system, 'api')
                        WHEN 'api' THEN 0
                        WHEN 'csv' THEN 1
                        ELSE 2
                    END,
                    COALESCE(t.source_record_key, t.transaction_id),
                    COALESCE(t.created_at, NOW()),
                    t.id
            ) AS rn
        FROM transactions t
    )
    SELECT
        account_id_key,
        transaction_id,
        transaction_date,
        settlement_date,
        transaction_type,
        symbol,
        quantity,
        price,
        amount,
        fee,
        raw
    FROM ranked
    WHERE rn = 1
    ORDER BY transaction_date, transaction_id
"""


def build_ledger(full_rebuild=False):
    """Populate ledger from canonical transactions.

    full_rebuild=True truncates ledger CASCADE before repopulating, resetting
    surrogate IDs. Otherwise, only the transaction-sourced slice is rebuilt.
    """
    backfill_stats = backfill_transaction_provenance()
    if backfill_stats["updated"]:
        print(f"  ledger: backfilled provenance for {backfill_stats['updated']} transaction row(s)")

    with get_connection() as conn:
        with conn.cursor() as cur:
            if full_rebuild:
                cur.execute("TRUNCATE TABLE ledger RESTART IDENTITY CASCADE")
                print("  ledger: truncated (full rebuild)")
            else:
                # open_lots and realized_gains FK-reference ledger.id and are
                # always fully rebuilt after build_ledger(), so clear them first.
                cur.execute("TRUNCATE TABLE open_lots, realized_gains CASCADE")
                print("  ledger: cleared open_lots + realized_gains (run build_realized_pnl to restore)")
                cur.execute("DELETE FROM ledger WHERE source_table = 'transactions'")

            cur.execute(_CANONICAL_TRANSACTIONS_SQL)
            rows = cur.fetchall()

            count = 0
            for (acct, txn_id, txn_date, settlement_date, txn_type,
                 symbol, quantity, price, amount, fee, raw) in rows:

                event_type = _map_event_type(txn_type, amount)

                # Buys: positive quantity (shares acquired)
                # Sells: negative quantity (shares disposed)
                # Cash events: no quantity
                if event_type in ("buy", "sell", "split", "redemption"):
                    if event_type == "sell" and quantity is not None:
                        qty = -abs(quantity)
                    elif quantity is not None:
                        qty = abs(quantity)
                    else:
                        qty = None
                else:
                    qty = None

                gross = abs(qty * price) if qty is not None and price is not None else None

                cur.execute(_UPSERT, (
                    acct,
                    txn_date,          # event_timestamp (TIMESTAMPTZ from transactions)
                    settlement_date,
                    event_type,
                    symbol,
                    qty,
                    price,
                    gross,
                    amount,
                    fee or 0,
                    txn_id,
                    json.dumps(raw) if isinstance(raw, dict) else raw,
                ))
                count += 1

    print(f"  ledger: upserted {count} canonical row(s) from transactions")

    return count
