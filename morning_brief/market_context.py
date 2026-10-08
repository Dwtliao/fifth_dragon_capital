"""Daily market history and calculated context; no LLM or database writes."""

from __future__ import annotations

import datetime
import math
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf


MARKET_GROUPS = {
    "global": {
        "Nikkei 225": "^N225", "Hang Seng": "^HSI", "Shanghai": "000001.SS",
        "India Nifty 50": "^NSEI", "ASX 200": "^AXJO", "Euro Stoxx 50": "^STOXX50E",
        "DAX": "^GDAXI", "FTSE 100": "^FTSE", "CAC 40": "^FCHI",
        "Canada TSX": "^GSPTSE", "Brazil Bovespa": "^BVSP",
    },
    "us_futures": {
        "Nasdaq futures": "NQ=F", "S&P 500 futures": "ES=F",
        "Dow futures": "YM=F", "Russell 2000 futures": "RTY=F",
    },
    "rates": {
        "13-week bill quoted yield": "^IRX", "5-year Treasury yield": "^FVX",
        "10-year Treasury yield": "^TNX", "30-year Treasury yield": "^TYX",
        "10-year note futures": "ZN=F", "30-year bond futures": "ZB=F",
        "IEF (7–10Y Treasury ETF)": "IEF", "TLT (20+Y Treasury ETF)": "TLT",
        "TIP (inflation-linked bond ETF)": "TIP",
    },
    "energy": {
        "WTI crude": "CL=F", "Brent crude": "BZ=F", "Natural gas": "NG=F",
        "Gasoline": "RB=F", "Heating oil": "HO=F",
    },
    "commodities": {
        "Gold": "GC=F", "Silver": "SI=F", "Copper": "HG=F",
        "Platinum": "PL=F", "Palladium": "PA=F", "Corn": "ZC=F",
        "Wheat": "ZW=F", "Soybeans": "ZS=F",
        "URA (uranium equity ETF)": "URA", "URNM (uranium miners ETF)": "URNM",
    },
    "currencies": {
        "US dollar index": "DX-Y.NYB", "EUR/USD": "EURUSD=X",
        "USD/JPY": "JPY=X", "AUD/USD": "AUDUSD=X", "USD/CNY": "CNY=X",
    },
    "volatility": {"VIX": "^VIX", "VVIX": "^VVIX", "VIXY": "VIXY"},
    "risk": {
        "SPY (S&P 500)": "SPY", "RSP (equal-weight S&P 500)": "RSP",
        "QQQ (Nasdaq 100)": "QQQ", "IWM (small caps)": "IWM",
        "SMH (semiconductors)": "SMH", "XLE (energy equities)": "XLE",
        "XLF (financials)": "XLF", "XLU (utilities)": "XLU",
        "XLP (consumer staples)": "XLP", "HYG (high-yield bonds)": "HYG",
        "LQD (investment-grade bonds)": "LQD",
    },
}

# Yahoo's Treasury indices are quoted in percentage points (e.g. 4.25%).
# ETF and futures prices are prices, never yield substitutes.
YIELD_TICKERS = {"^IRX", "^FVX", "^TNX", "^TYX"}


def _change(current, previous):
    return (current / previous - 1) * 100 if previous != 0 else None


def summarize_history(label, ticker, history, today):
    """Calculate on each instrument's valid bars, not the union calendar."""
    closes = pd.to_numeric(history["Close"], errors="coerce")
    closes = closes.where(closes.map(lambda v: pd.notna(v) and math.isfinite(v))).dropna().sort_index()
    if len(closes) < 2:
        return {"label": label, "ticker": ticker, "error": "insufficient daily history"}
    last = float(closes.iloc[-1])
    as_of = closes.index[-1].date()
    is_yield = ticker in YIELD_TICKERS
    row = {
        "label": label, "ticker": ticker, "last": last,
        "unit": "yield_pct" if is_yield else "price",
        "as_of": as_of.isoformat(), "age_days": (today - as_of).days,
        "stale": (today - as_of).days > 4,
    }
    for sessions in (1, 5, 20):
        previous = float(closes.iloc[-sessions - 1]) if len(closes) > sessions else None
        row[f"change_{sessions}d"] = (
            (last - previous) * 100 if is_yield else _change(last, previous)
        ) if previous is not None else None
    for sessions in (20, 50):
        average = float(closes.tail(sessions).mean()) if len(closes) >= sessions else None
        row[f"ma{sessions}"] = average
        row[f"above_ma{sessions}"] = last > average if average is not None else None
    if len(closes) >= 20:
        # Close range is intentional: comparable for yields, ETFs and futures.
        window = closes.tail(20)
        row["low_20d"] = float(window.min())
        row["high_20d"] = float(window.max())
    else:
        row["low_20d"] = row["high_20d"] = None
    return row


def build_cross_market_signals(histories, today):
    """Align dates before comparisons; never combine mismatched snapshots."""
    specs = (
        ("30Y minus 10Y Treasury yield", "^TYX", "^TNX", "yield_gap", "bp"),
        ("10Y minus 5Y Treasury yield", "^TNX", "^FVX", "yield_gap", "bp"),
        ("Brent minus WTI", "BZ=F", "CL=F", "difference", "USD/barrel"),
        ("Gold / silver", "GC=F", "SI=F", "ratio", "ratio"),
        ("Equal weight / S&P 500", "RSP", "SPY", "ratio", "ratio"),
        ("Small caps / S&P 500", "IWM", "SPY", "ratio", "ratio"),
        ("Nasdaq 100 / S&P 500", "QQQ", "SPY", "ratio", "ratio"),
        ("Semiconductors / S&P 500", "SMH", "SPY", "ratio", "ratio"),
        ("High-yield / investment-grade bond ETFs", "HYG", "LQD", "ratio", "ratio"),
    )
    signals = []
    for label, numerator, denominator, kind, unit in specs:
        if numerator not in histories or denominator not in histories:
            signals.append({"label": label, "error": "source history unavailable"})
            continue
        aligned = pd.concat([histories[numerator]["Close"], histories[denominator]["Close"]], axis=1)
        aligned.columns = ["a", "b"]
        aligned = aligned.apply(pd.to_numeric, errors="coerce").replace([float("inf"), -float("inf")], float("nan")).dropna().sort_index()
        if kind == "ratio":
            aligned = aligned[aligned["b"] != 0]
        if aligned.empty:
            signals.append({"label": label, "error": "no overlapping daily history"})
            continue
        if kind == "ratio":
            values = aligned["a"] / aligned["b"]
        else:
            values = (aligned["a"] - aligned["b"]) * (100 if kind == "yield_gap" else 1)
        last = float(values.iloc[-1])
        as_of = values.index[-1].date()
        row = {"label": label, "last": last, "unit": unit, "as_of": as_of.isoformat(),
               "stale": (today - as_of).days > 4}
        for sessions in (1, 5, 20):
            previous = float(values.iloc[-sessions - 1]) if len(values) > sessions else None
            row[f"change_{sessions}d"] = (
                _change(last, previous) if kind == "ratio" else last - previous
            ) if previous is not None else None
        signals.append(row)
    return signals


def fetch_market_overview():
    """One batched download for all market groups, with per-symbol failures."""
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    symbols = {ticker for group in MARKET_GROUPS.values() for ticker in group.values()}
    histories = {}
    download_error = None
    try:
        data = yf.download(
            sorted(symbols), period="6mo", interval="1d", group_by="ticker",
            auto_adjust=True, progress=False, threads=8, timeout=10,
        )
        if data is not None and not data.empty:
            for ticker in symbols:
                if ticker in data.columns.get_level_values(0):
                    histories[ticker] = data[ticker]
    except Exception as exc:
        download_error = str(exc)

    groups = {}
    for name, group in MARKET_GROUPS.items():
        rows = []
        for label, ticker in group.items():
            try:
                if ticker not in histories:
                    raise ValueError(download_error or "daily history unavailable")
                rows.append(summarize_history(label, ticker, histories[ticker], now.date()))
            except Exception as exc:
                rows.append({"label": label, "ticker": ticker, "error": str(exc)})
        groups[name] = rows
    return {"fetched_at": now.isoformat(), "groups": groups,
            "signals": build_cross_market_signals(histories, now.date())}
