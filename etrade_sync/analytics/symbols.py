import re
import time

import yfinance as yf

from etrade_sync.db import get_connection

# CUSIPs are 9-char alphanumeric identifiers starting with a digit.
# Treasury notes (912797*), corporate bonds, etc. yfinance won't resolve these.
_CUSIP_RE = re.compile(r'^\d[A-Z0-9]{8}$')

# Known internal/non-market symbols that yfinance cannot resolve
_SKIP_SYMBOLS = {"MSBNK", "MSPBNA", "RBL", "RPI", "RXD"}

_UPSERT = """
    INSERT INTO dim_symbols (symbol, name, sector, industry, asset_class, exchange, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (symbol) DO UPDATE SET
        name       = COALESCE(EXCLUDED.name,       dim_symbols.name),
        sector     = COALESCE(EXCLUDED.sector,     dim_symbols.sector),
        industry   = COALESCE(EXCLUDED.industry,   dim_symbols.industry),
        asset_class= COALESCE(EXCLUDED.asset_class,dim_symbols.asset_class),
        exchange   = COALESCE(EXCLUDED.exchange,   dim_symbols.exchange),
        updated_at = NOW()
"""


def _all_symbols():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT symbol FROM positions  WHERE symbol IS NOT NULL
                UNION
                SELECT DISTINCT symbol FROM transactions WHERE symbol IS NOT NULL AND symbol != ''
            """)
            return [r[0] for r in cur.fetchall()]


def _fetch_yfinance(symbol):
    """Return (name, sector, industry, asset_class, exchange) or Nones on failure."""
    try:
        info = yf.Ticker(symbol).info
        quote_type = info.get("quoteType", "").upper()

        asset_class_map = {
            "EQUITY":      "Equity",
            "ETF":         "ETF",
            "MUTUALFUND":  "ETF",
            "BOND":        "Bond",
            "FUTURE":      "Future",
            "CURRENCY":    "Currency",
            "CRYPTOCURRENCY": "Crypto",
        }
        asset_class = asset_class_map.get(quote_type, "Equity")

        return (
            info.get("longName") or info.get("shortName"),
            info.get("sector"),
            info.get("industry"),
            asset_class,
            info.get("exchange"),
        )
    except Exception:
        return (None, None, None, None, None)


def seed_symbols():
    """Fetch metadata for all known symbols via yfinance. Safe to re-run."""
    symbols = _all_symbols()
    print(f"  dim_symbols: {len(symbols)} symbols to process")

    seeded = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for sym in symbols:
                if _CUSIP_RE.match(sym):
                    cur.execute(_UPSERT, (sym, None, "Fixed Income", None, "Bond", None))
                    seeded += 1
                    continue

                if sym in _SKIP_SYMBOLS:
                    cur.execute(_UPSERT, (sym, sym, None, None, "Other", None))
                    seeded += 1
                    continue

                name, sector, industry, asset_class, exchange = _fetch_yfinance(sym)
                cur.execute(_UPSERT, (sym, name, sector, industry, asset_class, exchange))
                seeded += 1
                print(f"    {sym}: {asset_class or '?'} | {sector or '—'}")
                time.sleep(0.3)   # be polite to Yahoo Finance

    print(f"  dim_symbols: seeded {seeded} row(s)")
    return seeded
