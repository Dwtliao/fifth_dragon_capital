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


def fetch_positions(key_levels: dict) -> list[dict]:
    """
    Fetch positions for the morning brief.

    Source priority:
      1. DB (mv_unrealized_pnl) — authoritative ticker list + cost basis
      2. key_levels.yml positions section — stop levels and notes layered on top
      3. Fallback: YAML-only if DB is unavailable

    Each returned dict includes:
      label, ticker, last, prior, pct,        ← from yfinance
      cost_basis, market_value, unrealized_pnl_pct  ← from DB (or None)
      stop, note, warn                         ← from key_levels.yml
    """
    pos_config = dict(key_levels.get("positions") or {})

    # ── Try DB first ──────────────────────────────────────────────────────────
    db_rows    = fetch_positions_from_db()
    db_by_sym  : dict[str, dict] = {}
    db_ok      = db_rows and "error" not in db_rows[0]

    if db_ok:
        for row in db_rows:
            db_by_sym[row["symbol"].upper()] = row
        # Union: DB tickers + any YAML tickers not in DB (carry-over / non-brokerage)
        all_tickers = sorted(set(db_by_sym.keys()) | set(pos_config.keys()))
    else:
        all_tickers = sorted(pos_config.keys())

    if not all_tickers:
        return []

    # ── Fetch live prices via yfinance ────────────────────────────────────────
    symbols = {t: t for t in all_tickers}
    snaps   = _fetch_snapshot(symbols)

    # ── Merge ─────────────────────────────────────────────────────────────────
    enriched = []
    for snap in snaps:
        ticker = snap["ticker"].upper()
        yaml   = pos_config.get(ticker, {})
        db     = db_by_sym.get(ticker, {})

        stop = yaml.get("stop")
        note = yaml.get("note", "")
        last = snap.get("last")

        warn = ""
        if stop and last:
            dist_pct = (last - stop) / stop * 100
            if dist_pct < 3:
                warn = f"⚠ {dist_pct:.1f}% above stop {stop}"

        enriched.append({
            **snap,
            # DB enrichment (None if DB unavailable or ticker not in DB)
            "cost_basis":          db.get("cost_basis"),
            "market_value_db":     db.get("market_value"),
            "unrealized_pnl":      db.get("unrealized_pnl"),
            "unrealized_pnl_pct":  db.get("unrealized_pnl_pct"),
            "quantity":            db.get("quantity"),
            # YAML metadata
            "stop": stop,
            "note": note,
            "warn": warn,
        })

    return enriched


def fetch_watch_levels(key_levels: dict) -> list[dict]:
    """
    Fetch prices for tickers in key_levels['watch'] and compute distance
    to support/resistance.
    """
    watch_config = key_levels.get("watch", {})
    if not watch_config:
        return []

    # Map display names → yfinance tickers
    WATCH_TICKERS = {
        "NQ":  "NQ=F",
        "GC":  "GC=F",
        "VIX": "^VIX",
        "FNV": "FNV",
        "DXY": "DX-Y.NYB",
    }

    symbols = {k: WATCH_TICKERS.get(k, k) for k in watch_config}
    snaps   = _fetch_snapshot(symbols)

    enriched = []
    for snap in snaps:
        label  = snap["label"]
        config = watch_config.get(label, {})
        last   = snap.get("last")
        notes  = []

        if last:
            sup  = config.get("support")
            res  = config.get("resistance")
            alrt = config.get("alert_above")

            if res:
                dist = round(res - last, 2)
                notes.append(f"Resistance {res} → {dist:+.2f} ({_safe_pct(res, last):+.1f}%)" if _safe_pct(res, last) else f"Resistance {res}")
            if sup:
                dist = round(last - sup, 2)
                notes.append(f"Support {sup} → {dist:+.2f} away" if dist >= 0 else f"⚠ Below support {sup}")
            if alrt and last > alrt:
                notes.append(f"⚠ Above alert level {alrt}")

        enriched.append({
            **snap,
            "key_note": config.get("note", ""),
            "level_notes": notes,
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
                    symbol,
                    quantity,
                    cost_basis,
                    market_value,
                    unrealized_pnl,
                    unrealized_pnl_pct
                FROM mv_unrealized_pnl
                WHERE quantity IS NOT NULL AND quantity > 0
                ORDER BY market_value DESC NULLS LAST
            """)
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
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

    updated = {**key_levels, "positions": existing_positions}
    return updated, added, removed


def fetch_fed_events() -> list[dict]:
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
