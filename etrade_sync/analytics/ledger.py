import json

from etrade_sync.db import get_connection

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


def build_ledger(full_rebuild=False):
    """Populate ledger from transactions table. Safe to re-run (upsert).

    full_rebuild=True truncates ledger CASCADE before repopulating, resetting
    surrogate IDs. Use this after mapping changes or schema fixes. Dependent
    fact tables (fact_transactions, fact_cashflows) are also cleared by CASCADE.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if full_rebuild:
                cur.execute("TRUNCATE TABLE ledger RESTART IDENTITY CASCADE")
                print("  ledger: truncated (full rebuild)")

            cur.execute("""
                SELECT account_id_key, transaction_id, transaction_date,
                       settlement_date, transaction_type, symbol,
                       quantity, price, amount, fee, raw
                FROM transactions
                ORDER BY transaction_date
            """)
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

    print(f"  ledger: upserted {count} row(s) from transactions")
    return count
