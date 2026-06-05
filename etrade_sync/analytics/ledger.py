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

    # Dedup guard — two passes per event class:
    #   Pass A (cross-source): removes CSV rows that duplicate an API row.
    #                          Uses ROUND(price, 2) because CSV exports only
    #                          carry 2 decimal places (e.g. $11.50 vs $11.4997).
    #   Pass B (same-source):  removes API rows that E*TRADE returned twice under
    #                          different transaction_ids. Uses exact price so that
    #                          two genuine fills at slightly different API prices
    #                          are never collapsed.
    with get_connection() as conn:
        with conn.cursor() as cur:

            # ── fills (buy / sell) ────────────────────────────────────────────

            # A: CSV rows shadowed by an API row (price comparison at 2dp)
            cur.execute("""
                DELETE FROM ledger
                WHERE id IN (
                    SELECT a.id
                    FROM   ledger a
                    JOIN   ledger b
                           ON  a.account_id_key            = b.account_id_key
                           AND a.event_type                = b.event_type
                           AND a.symbol                    = b.symbol
                           AND a.event_timestamp::date     = b.event_timestamp::date
                           AND a.quantity                  = b.quantity
                           AND ROUND(a.price::numeric, 2)  = ROUND(b.price::numeric, 2)
                    WHERE  a.event_type IN ('buy', 'sell')
                      AND  a.symbol   IS NOT NULL
                      AND  a.quantity IS NOT NULL
                      AND  a.price    IS NOT NULL
                      AND  a.source_id LIKE 'CSV:%'
                      AND  b.source_id NOT LIKE 'CSV:%'
                )
            """)
            csv_fill_dupes = cur.rowcount

            # B: API rows the E*TRADE API returned twice (exact price match)
            cur.execute("""
                DELETE FROM ledger
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY account_id_key, event_type, symbol,
                                                event_timestamp::date, quantity, price
                                   ORDER BY id
                               ) AS rn
                        FROM ledger
                        WHERE event_type IN ('buy', 'sell')
                          AND symbol      IS NOT NULL
                          AND quantity    IS NOT NULL
                          AND price       IS NOT NULL
                          AND source_id NOT LIKE 'CSV:%'
                    ) ranked
                    WHERE rn > 1
                )
            """)
            api_fill_dupes = cur.rowcount
            buy_sell_dupes = csv_fill_dupes + api_fill_dupes

            # ── cash events (dividend, interest, fee) ─────────────────────────

            # A: CSV rows shadowed by an API row
            cur.execute("""
                DELETE FROM ledger
                WHERE id IN (
                    SELECT a.id
                    FROM   ledger a
                    JOIN   ledger b
                           ON  a.account_id_key        = b.account_id_key
                           AND a.event_type            = b.event_type
                           AND a.symbol IS NOT DISTINCT FROM b.symbol
                           AND a.event_timestamp::date = b.event_timestamp::date
                           AND a.net_amount            = b.net_amount
                    WHERE  a.event_type IN ('dividend', 'dividend_qualified', 'interest', 'fee')
                      AND  a.net_amount IS NOT NULL
                      AND  a.source_id     LIKE 'CSV:%'
                      AND  b.source_id NOT LIKE 'CSV:%'
                )
            """)
            cash_dupes = cur.rowcount

            # B: API rows the E*TRADE API returned twice
            cur.execute("""
                DELETE FROM ledger
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY account_id_key, event_type, symbol,
                                                event_timestamp::date, net_amount
                                   ORDER BY id
                               ) AS rn
                        FROM ledger
                        WHERE event_type IN ('dividend', 'dividend_qualified', 'interest', 'fee')
                          AND net_amount IS NOT NULL
                          AND source_id NOT LIKE 'CSV:%'
                    ) ranked
                    WHERE rn > 1
                )
            """)
            cash_dupes += cur.rowcount

            # ── splits ────────────────────────────────────────────────────────

            cur.execute("""
                DELETE FROM ledger
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY account_id_key, symbol,
                                                event_timestamp::date, quantity
                                   ORDER BY id
                               ) AS rn
                        FROM ledger
                        WHERE event_type = 'split'
                    ) ranked
                    WHERE rn > 1
                )
            """)
            cash_dupes += cur.rowcount

            dupes = buy_sell_dupes + cash_dupes
            if dupes:
                print(f"  ledger: removed {dupes} duplicate(s) ({buy_sell_dupes} fills, {cash_dupes} cash events)")

    return count
