import time

import yfinance as yf

from etrade_sync.db import get_connection

BENCHMARKS = ["SPY"]
FETCH_START = "2020-01-01"


def seed_prices():
    """Fetch adjusted daily closing prices for benchmark symbols via yfinance.

    Upserts into market_prices. Safe to re-run — existing rows are updated
    if yfinance returns a revised close (e.g. after a dividend adjustment).
    Returns total rows upserted.
    """
    total = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for symbol in BENCHMARKS:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(start=FETCH_START, auto_adjust=True)

                rows = [
                    (symbol, ts.date(), float(row["Close"]))
                    for ts, row in hist.iterrows()
                ]

                cur.executemany(
                    """
                    INSERT INTO market_prices (symbol, date, close)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (symbol, date) DO UPDATE SET
                        close = EXCLUDED.close
                    """,
                    rows,
                )

                print(f"  market_prices: {len(rows)} rows upserted for {symbol}")
                total += len(rows)
                time.sleep(0.3)

    return total
