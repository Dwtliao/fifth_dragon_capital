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
    Fetch last prices for tickers listed under key_levels['positions'].
    Cross-references stop levels from key_levels.yml and flags warnings.
    """
    pos_config = key_levels.get("positions", {})
    if not pos_config:
        return []

    symbols = {ticker: ticker for ticker in pos_config}
    snaps   = _fetch_snapshot(symbols)

    enriched = []
    for snap in snaps:
        ticker  = snap["ticker"]
        config  = pos_config.get(ticker, {})
        stop    = config.get("stop")
        note    = config.get("note", "")
        last    = snap.get("last")

        warn = ""
        if stop and last:
            dist_pct = (last - stop) / stop * 100
            if dist_pct < 3:
                warn = f"⚠ {dist_pct:.1f}% above stop {stop}"

        enriched.append({**snap, "stop": stop, "note": note, "warn": warn})

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
