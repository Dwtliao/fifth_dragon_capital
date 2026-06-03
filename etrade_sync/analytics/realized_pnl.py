import re
from collections import defaultdict
from decimal import Decimal

from etrade_sync.db import get_connection

_SPLIT_RATIO_RE = re.compile(
    r'SPLIT RATIO\s+(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)', re.IGNORECASE
)


def _ratio_from_description(raw):
    """Parse split ratio from E*TRADE description string (e.g. 'SPLIT RATIO 10:1')."""
    if not isinstance(raw, dict):
        return None
    desc = raw.get('description', '') or ''
    m = _SPLIT_RATIO_RE.search(desc)
    if m:
        return Decimal(m.group(1)) / Decimal(m.group(2))
    return None


def build_realized_pnl():
    """FIFO cost basis matching with split-ratio adjustment.

    For each (account, symbol), applies cumulative split ratios to buy lot
    quantities and prices before matching against sell events. Buy lots that
    predate a split have their per-share price divided by the ratio and their
    share count multiplied by the ratio, so proceeds and cost basis are
    compared in the same post-split units.

    Truncates realized_gains and rebuilds from ledger. Returns lot count.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, account_id_key, symbol, event_timestamp::date,
                       event_type, price, quantity, raw
                FROM ledger
                WHERE event_type IN ('buy', 'sell', 'split')
                  AND symbol IS NOT NULL
                  AND quantity IS NOT NULL
                ORDER BY account_id_key, symbol, event_timestamp
            """)
            rows = cur.fetchall()

    # Group events per (account, symbol) in chronological order
    events_by_key = defaultdict(list)
    for ledger_id, account, symbol, date, event_type, price, quantity, raw in rows:
        events_by_key[(account, symbol)].append(
            (date, event_type, Decimal(str(quantity)), price, ledger_id, raw)
        )

    buys = defaultdict(list)   # key → [[id, date, price, original_qty], ...]
    sells = defaultdict(list)  # key → [(id, date, price, qty), ...]
    splits = defaultdict(list) # key → [(split_date, ratio), ...]

    for key, key_events in events_by_key.items():
        running_pos = Decimal('0')
        for date, event_type, qty, price, ledger_id, raw in key_events:
            if event_type == 'buy' and price is not None:
                buys[key].append([ledger_id, date, Decimal(str(price)), qty])
                running_pos += qty
            elif event_type == 'sell' and price is not None:
                sells[key].append((ledger_id, date, Decimal(str(price)), abs(qty)))
                running_pos += qty  # qty is negative
            elif event_type == 'split':
                if running_pos > 0:
                    ratio = (running_pos + qty) / running_pos
                else:
                    ratio = _ratio_from_description(raw)
                if ratio and ratio > 0:
                    splits[key].append((date, ratio))
                    print(f"  split: {key[1]} on {date} ratio={float(ratio):.4f}")
                else:
                    print(f"  WARNING: cannot determine split ratio for {key[1]} on {date} — skipping")
                running_pos += qty

    # FIFO matching
    lots = []
    for key, key_sells in sells.items():
        account, symbol = key
        key_splits = splits.get(key, [])
        buy_queue = [[b[0], b[1], b[2], b[3]] for b in buys.get(key, [])]
        buy_ptr = 0

        for sell_id, sell_date, sell_price, sell_qty in key_sells:
            remaining = sell_qty
            while remaining > 0 and buy_ptr < len(buy_queue):
                b = buy_queue[buy_ptr]  # [id, buy_date, original_price, remaining_original_qty]

                # Cumulative split ratio for all splits between this buy date and sell date
                adj_ratio = Decimal('1')
                for split_date, ratio in key_splits:
                    if b[1] < split_date <= sell_date:
                        adj_ratio *= ratio

                adj_qty = b[3] * adj_ratio      # available shares in post-split units
                adj_price = b[2] / adj_ratio    # per-share cost in post-split units

                matched = min(remaining, adj_qty)
                cost_basis = matched * adj_price
                proceeds = matched * sell_price
                holding_days = (sell_date - b[1]).days

                lots.append((
                    account, symbol,
                    b[0], b[1], float(adj_price),
                    sell_id, sell_date, float(sell_price),
                    float(matched), float(cost_basis), float(proceeds),
                    float(proceeds - cost_basis),
                    holding_days, "long" if holding_days >= 365 else "short",
                ))

                # Reduce original (pre-split) qty by the equivalent original shares consumed
                b[3] -= matched / adj_ratio
                remaining -= matched
                if b[3] <= 0:
                    buy_ptr += 1

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
