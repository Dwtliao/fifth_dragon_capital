"""
fetchers.py — data-pull functions for the morning brief.

Each function returns a plain dict (or list of dicts) that formatter.py
consumes.  All failures are caught and returned as {"error": "..."} so a
single bad ticker or network hiccup does not kill the whole brief.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Optional

import yfinance as yf

# ── symbol maps ──────────────────────────────────────────────────────────────

GLOBAL_INDICES = {
    "Nikkei":   "^N225",
    "DAX":      "^GDAXI",
    "FTSE":     "^FTSE",
    "Hang Seng":"^HSI",
    "ASX 200":  "^AXJO",
}

US_FUTURES = {
    "NQ (Nasdaq)": "NQ=F",
    "ES (S&P 500)": "ES=F",
    "YM (Dow)":    "YM=F",
    "RTY (Russell)": "RTY=F",
}

COMMODITIES = {
    "Gold":    "GC=F",
    "Silver":  "SI=F",
    "Oil WTI": "CL=F",
    "Copper":  "HG=F",
    "Uranium (URA)": "URA",
}

CURRENCIES = {
    "DXY":     "DX-Y.NYB",
    "JPY/USD (FXY)": "FXY",
    "AUD/USD": "AUDUSD=X",
    "EUR/USD": "EURUSD=X",
}

VOL_PROXIES = {
    "VIX":   "^VIX",
    "VVIX":  "^VVIX",
    "VIXY":  "VIXY",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_pct(current: float, prior: float) -> Optional[float]:
    if prior and prior != 0:
        return round((current - prior) / prior * 100, 2)
    return None


def _fetch_snapshot(symbols: dict[str, str]) -> list[dict]:
    """Fetch last close + prior close for a dict of {label: ticker}."""
    results = []
    tickers = list(symbols.values())
    try:
        data = yf.download(
            tickers,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        return [{"label": lbl, "ticker": tkr, "error": str(exc)}
                for lbl, tkr in symbols.items()]

    for label, ticker in symbols.items():
        try:
            if len(tickers) == 1:
                closes = data["Close"].dropna()
            else:
                closes = data[ticker]["Close"].dropna()

            if len(closes) < 2:
                results.append({"label": label, "ticker": ticker, "error": "insufficient data"})
                continue

            last  = float(closes.iloc[-1])
            prior = float(closes.iloc[-2])
            pct   = _safe_pct(last, prior)
            results.append({
                "label":  label,
                "ticker": ticker,
                "last":   round(last, 2),
                "prior":  round(prior, 2),
                "pct":    pct,
                "as_of": closes.index[-1].date().isoformat(),
            })
        except Exception as exc:
            results.append({"label": label, "ticker": ticker, "error": str(exc)})

    return results


# ── public fetch functions ────────────────────────────────────────────────────

def fetch_global_indices() -> list[dict]:
    """Overnight global equity indices."""
    return _fetch_snapshot(GLOBAL_INDICES)


def fetch_us_futures() -> list[dict]:
    """US equity index futures (pre-market direction)."""
    return _fetch_snapshot(US_FUTURES)


def fetch_commodities() -> list[dict]:
    """Key commodity futures."""
    return _fetch_snapshot(COMMODITIES)


def fetch_currencies() -> list[dict]:
    """FX + DXY."""
    return _fetch_snapshot(CURRENCIES)


def fetch_vol_proxies() -> list[dict]:
    """VIX, VVIX, VIXY."""
    return _fetch_snapshot(VOL_PROXIES)


# Brief-only exclusions: internal broker codes RBL/RPI and user-requested BR.
# Do not delete their broker history or treat this as a global symbol blacklist.
BRIEF_POSITION_EXCLUSIONS = frozenset({'RBL', 'RPI', 'BR'})


def fetch_positions(key_levels: dict) -> list[dict]:
    """
    Fetch positions for the morning brief.

    Source priority:
      1. DB (mv_unrealized_pnl) — authoritative ticker list + cost basis
      2. key_levels.yml positions section — stop levels and notes layered on top
      Saved metadata never adds tickers absent from current holdings.

    Each returned dict includes:
      label, ticker, last, prior, pct,        ← from yfinance
      cost_basis, market_value, unrealized_pnl_pct  ← from DB (or None)
      stop, note, warn                         ← from key_levels.yml
    """
    pos_config = dict(key_levels.get("positions") or {})

    # ── Try DB first ──────────────────────────────────────────────────────────
    db_rows    = fetch_positions_from_db()
    db_by_sym  : dict[str, dict] = {}
    if any('error' in row for row in db_rows):
        raise RuntimeError('Current holdings unavailable; refusing to display saved metadata as holdings')
    for row in db_rows:
        if float(row.get('quantity') or 0) > 0 and row['symbol'].upper() not in BRIEF_POSITION_EXCLUSIONS:
            db_by_sym[row["symbol"].upper()] = row
    all_tickers = sorted(db_by_sym)

    if not all_tickers:
        return []

    # ── Try E*TRADE quotes first (real-time); fall back to yfinance silently ──
    etrade_quotes: dict[str, dict] = {}
    try:
        from etrade_sync.market.quotes import get_quotes_safe
        etrade_quotes, _err = get_quotes_safe(all_tickers)
    except Exception:
        pass  # token missing or import error — yfinance handles everything

    # yfinance fallback for tickers not covered by E*TRADE
    yf_tickers = [t for t in all_tickers if t not in etrade_quotes]
    yf_snaps   = {s["ticker"].upper(): s for s in _fetch_snapshot({t: t for t in yf_tickers})} \
                 if yf_tickers else {}

    # ── Merge ─────────────────────────────────────────────────────────────────
    enriched = []
    for ticker in all_tickers:
        yaml_cfg = pos_config.get(ticker, {})
        db       = db_by_sym.get(ticker, {})

        # Price: E*TRADE preferred, yfinance fallback
        eq = etrade_quotes.get(ticker)
        yf = yf_snaps.get(ticker, {})

        if eq:
            last    = eq["last_price"]
            pct     = eq.get("change_pct")
            source  = "etrade"
        elif "last" in yf:
            last    = yf["last"]
            pct     = yf.get("pct")
            source  = "yfinance"
        else:
            last    = None
            pct     = None
            source  = "error"

        stop = yaml_cfg.get("stop")
        note = yaml_cfg.get("note", "")

        warn = ""
        if stop and last:
            dist_pct = (last - stop) / stop * 100
            if dist_pct < 3:
                warn = f"⚠ {dist_pct:.1f}% above stop {stop}"

        snap = yf if yf else {"label": ticker, "ticker": ticker}
        enriched.append({
            **snap,
            "label":               ticker,
            "ticker":              ticker,
            "last":                last,
            "pct":                 pct,
            "price_source":        source,
            "cost_basis":          db.get("cost_basis"),
            "market_value_db":     db.get("market_value"),
            "unrealized_pnl":      db.get("unrealized_pnl"),
            "unrealized_pnl_pct":  db.get("unrealized_pnl_pct"),
            "quantity":            db.get("quantity"),
            "stop":                stop,
            "note":                note,
            "warn":                warn,
        })

    return enriched


def fetch_positions_from_db() -> list[dict]:
    """
    Query mv_unrealized_pnl for current holdings.
    Returns list of dicts: symbol, quantity, cost_basis, market_value,
    unrealized_pnl, unrealized_pnl_pct.
    Returns [] if DB is unavailable (brief still renders from key_levels.yml).
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from etrade_sync.db import get_connection  # type: ignore
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    u.symbol,
                    u.quantity,
                    u.cost_basis,
                    u.market_value,
                    u.unrealized_pnl,
                    u.unrealized_pnl_pct
                FROM mv_unrealized_pnl u
                JOIN (
                    SELECT DISTINCT symbol, security_type
                    FROM positions
                    WHERE (account_id_key, fetched_at) IN (
                        SELECT account_id_key, MAX(fetched_at) FROM positions GROUP BY account_id_key
                    )
                ) p ON p.symbol = u.symbol
                WHERE u.quantity IS NOT NULL AND u.quantity > 0
                  AND p.security_type = 'EQ'
                ORDER BY u.market_value DESC NULLS LAST
            """)
            cols = [d.name for d in cur.description]
            # Skip internal broker codes and explicit brief display exclusions.
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            rows = [row for row in rows if row['symbol'].upper() not in BRIEF_POSITION_EXCLUSIONS]
        conn.close()
        return rows
    except Exception as exc:
        return [{"error": str(exc)}]


def sync_positions_from_db(key_levels: dict) -> tuple[dict, list[str], list[str]]:
    """
    Smart merge: DB holdings → key_levels['positions'].

    Rules:
      - ADD tickers in DB but not in YAML (with blank stop/note)
      - REMOVE tickers in YAML but not in DB, ONLY if they have no stop and no note
        (tickers with user metadata are kept as a safety measure)
      - PRESERVE all existing stops, notes, and any other metadata

    Returns:
      (updated_key_levels, added_tickers, removed_tickers)
    """
    db_rows = fetch_positions_from_db()

    # If DB fetch failed entirely, bail out without touching YAML
    if db_rows and "error" in db_rows[0]:
        raise RuntimeError(f"DB unavailable: {db_rows[0]['error']}")

    db_symbols = {row["symbol"].upper() for row in db_rows}

    existing_positions = dict(key_levels.get("positions") or {})
    yaml_symbols = {t.upper() for t in existing_positions}

    added   = []
    removed = []

    # Add new tickers from DB
    for symbol in sorted(db_symbols - yaml_symbols):
        existing_positions[symbol] = {}
        added.append(symbol)

    # Remove closed positions — only if no user metadata attached
    for symbol in sorted(yaml_symbols - db_symbols):
        entry = existing_positions.get(symbol, {})
        has_metadata = bool(entry.get("stop")) or bool(str(entry.get("note", "")).strip())
        if not has_metadata:
            del existing_positions[symbol]
            removed.append(symbol)

    updated = {"positions": existing_positions}
    return updated, added, removed


def load_key_levels_from_db() -> dict:
    """Load position stops/notes only. Fail closed rather than saving an empty read.

    An entirely empty table may seed position metadata from YAML. Retired watch
    references in old YAML files are ignored and can never recreate saved levels.
    Works before and after migration 078 without reading watch-only columns.
    """
    from etrade_sync.db import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT section,ticker,stop,note FROM key_levels ORDER BY section,ticker")
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        yml_path = Path(__file__).parent / "key_levels.yml"
        if yml_path.exists():
            import yaml
            data = yaml.safe_load(yml_path.read_text()) or {}
            positions = {ticker: {k: v for k, v in (vals or {}).items() if k in ('stop','note')}
                         for ticker, vals in (data.get('positions') or {}).items()}
            if positions:
                save_key_levels_to_db({'positions': positions})
                return {'positions': positions}
    result = {'positions': {}}
    for section, ticker, stop, note in rows:
        if section != 'positions':
            continue
        entry = {}
        if stop is not None:
            entry['stop'] = float(stop)
        if note:
            entry['note'] = note
        result['positions'][ticker] = entry
    return result


def save_key_levels_to_db(key_levels: dict) -> None:
    """Save position metadata without changing identities or retired watch rows."""
    if key_levels.get('watch'):
        raise ValueError('Saved Watch Levels are retired; create an alert in P11 instead')
    positions = key_levels.get('positions') or {}
    if not positions:
        raise ValueError('Refusing to save empty positions: this replaces the whole positions table, not a diff')
    from etrade_sync.db import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM key_levels WHERE section='positions' AND NOT (ticker=ANY(%s))", (list(positions),))
            for ticker, vals in positions.items():
                vals = vals or {}
                cur.execute("""INSERT INTO key_levels(section,ticker,stop,note,updated_at)
                    VALUES ('positions',%s,%s,%s,NOW()) ON CONFLICT(section,ticker)
                    DO UPDATE SET stop=EXCLUDED.stop,note=EXCLUDED.note,updated_at=NOW()""",
                    (ticker,vals.get('stop'),vals.get('note')))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_fed_events() -> list:
    """
    Returns known/hardcoded near-term Fed events.
    In a future version this could scrape federalreserve.gov/meetings.htm.
    For now it reads from a local events.yml if present, else returns empty.
    """
    events_file = Path(__file__).parent / "events.yml"
    if not events_file.exists():
        return []

    try:
        import yaml  # optional dependency
        with open(events_file) as f:
            data = yaml.safe_load(f) or {}
        today = datetime.date.today()
        upcoming = []
        for event in data.get("events", []):
            try:
                event_date = datetime.date.fromisoformat(event["date"])
                days_away  = (event_date - today).days
                if -1 <= days_away <= 30:   # show today, tomorrow, and next 30 days
                    upcoming.append({**event, "days_away": days_away})
            except Exception:
                pass
        return sorted(upcoming, key=lambda e: e["days_away"])
    except ImportError:
        return []
    except Exception as exc:
        return [{"error": str(exc)}]
