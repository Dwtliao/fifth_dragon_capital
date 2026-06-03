from collections import defaultdict
from decimal import Decimal

from etrade_sync.db import get_connection


def build_realized_pnl():
    """FIFO cost basis matching for all (account, symbol) pairs.

    Truncates realized_gains and rebuilds from ledger buy/sell events.
    Splits are not currently factored into cost basis adjustment.
    Returns number of matched lots inserted.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, account_id_key, symbol, event_timestamp::date, price, quantity
                FROM ledger
                WHERE event_type IN ('buy', 'sell')
                  AND symbol IS NOT NULL
                  AND quantity IS NOT NULL
                  AND price IS NOT NULL
                ORDER BY account_id_key, symbol, event_timestamp
            """)
            rows = cur.fetchall()

    buys = defaultdict(list)
    sells = defaultdict(list)
    for ledger_id, account, symbol, date, price, quantity in rows:
        key = (account, symbol)
        qty = Decimal(str(quantity))
        px = Decimal(str(price))
        if qty > 0:
            buys[key].append([ledger_id, date, px, qty])
        else:
            sells[key].append((ledger_id, date, px, abs(qty)))

    lots = []
    for key, key_sells in sells.items():
        account, symbol = key
        buy_queue = [[b[0], b[1], b[2], b[3]] for b in buys.get(key, [])]
        buy_ptr = 0

        for sell_id, sell_date, sell_price, sell_qty in key_sells:
            remaining = sell_qty
            while remaining > 0 and buy_ptr < len(buy_queue):
                b = buy_queue[buy_ptr]
                matched = min(remaining, b[3])
                cost_basis = matched * b[2]
                proceeds = matched * sell_price
                holding_days = (sell_date - b[1]).days
                lots.append((
                    account, symbol,
                    b[0], b[1], float(b[2]),
                    sell_id, sell_date, float(sell_price),
                    float(matched), float(cost_basis), float(proceeds),
                    float(proceeds - cost_basis),
                    holding_days, "long" if holding_days >= 365 else "short",
                ))
                b[3] -= matched
                remaining -= matched
                if b[3] <= 0:
                    buy_ptr += 1
            # if remaining > 0: sell predates history window, lot is unmatched

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE realized_gains")
            if lots:
                cur.executemany(
                    """
                    INSERT INTO realized_gains
                        (account_id_key, symbol,
                         buy_ledger_id, buy_date, buy_price,
                         sell_ledger_id, sell_date, sell_price,
                         quantity, cost_basis, proceeds, realized_pnl,
                         holding_days, term)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    lots,
                )

    print(f"  realized_pnl: {len(lots)} FIFO lot(s) matched")
    return len(lots)
