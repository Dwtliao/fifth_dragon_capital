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

# Confirmation hierarchy — see PENDING TASKS. Tier 1 = leadership (moves first),
# Tier 2 = index confirmation (spreading), Tier 3 = macro confirmation (systemic fear).
# DRAM dropped: no reliable yfinance ticker for spot DRAM pricing: MU + Kioxia cover
# the memory-chip signal. MOVE (^MOVE) is sometimes spotty on Yahoo's feed — verify
# before relying on it.
CONFIRMATION_TIERS = {
    1: {
        "SOXS":           "SOXS",
        "SOXL":           "SOXL",
        "SMH":            "SMH",
        "Micron":         "MU",
        "Kioxia":         "285A.T",
    },
    2: {
        "Nasdaq 100":     "^NDX",
        "S&P 500":        "^GSPC",
        "SOX":            "^SOX",
        "Nikkei 225":     "^N225",
    },
    3: {
        "VIX":            "^VIX",
        "VVIX":           "^VVIX",
        "MOVE":           "^MOVE",
        "DXY":            "DX-Y.NYB",
    },
}

# Credit spreads have no single yfinance ticker — HYG/LQD price ratio stands in as a
# direction proxy (falling ratio ~ widening high-yield spreads) until a real data
# source (e.g. FRED) is worth the added infrastructure.
CREDIT_SPREAD_PROXY = ("HYG", "LQD")


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_pct(current: float, prior: float) -> Optional[float]:
    if prior and prior != 0:
        return round((current - prior) / prior * 100, 2)
    return None


def _true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 20) -> Optional[float]:
    """Simple (non-Wilder) average true range over the trailing `period` completed sessions."""
    if len(closes) < period + 1:
        return None
    tr = [_true_range(highs[i], lows[i], closes[i - 1]) for i in range(1, len(closes))]
    return sum(tr[-period:]) / period


def _latest_true_range(highs: list[float], lows: list[float], closes: list[float]) -> Optional[float]:
    if len(closes) < 2:
        return None
    return _true_range(highs[-1], lows[-1], closes[-2])


def _move_percentile(returns: list[float], latest_abs_return: float, window: int = 250) -> Optional[float]:
    """Percentile rank of the latest |return| within the trailing `window` sessions' |returns|."""
    sample = [abs(r) for r in returns[-window:]]
    if len(sample) < 30:
        return None
    return round(sum(1 for r in sample if r <= latest_abs_return) / len(sample) * 100, 1)


def _crossed_level(latest_high: float, latest_low: float,
                    rolling_high: Optional[float], rolling_low: Optional[float]) -> Optional[str]:
    """Did the latest completed session take out the prior rolling high/low?"""
    if rolling_high is not None and latest_high > rolling_high:
        return "20-day high"
    if rolling_low is not None and latest_low < rolling_low:
        return "20-day low"
    return None


def _observation_facts_from_ohlcv(
    closes: list[float], highs: list[float] | None, lows: list[float] | None,
    volumes: list[float] | None, current_price: float,
) -> dict:
    """Computes Observation-layer facts from a completed daily OHLCV series plus a fresher current price.

    All fields are None when the underlying series doesn't support them (e.g. no
    high/low/volume for a synthetic ratio series). No network calls — pure math,
    so this is unit-testable without yfinance.
    """
    prior_close = closes[-1]
    prior_prior_close = closes[-2] if len(closes) >= 2 else None

    facts: dict = {
        "last":               round(current_price, 4),
        "prior_close":        round(prior_close, 4),
        "overnight_return":   _safe_pct(current_price, prior_close),
        "session_return_1d":  _safe_pct(prior_close, prior_prior_close) if prior_prior_close else None,
        "dist_from_20dma":    None,
        "dist_from_50dma":    None,
        "range_expansion":    None,
        "volume_ratio":       None,
        "move_percentile":    None,
        "crossed_level":      None,
        "persistence":        None,  # deferred — needs overnight intraday bars, see PENDING TASKS
    }

    if len(closes) >= 20:
        facts["dist_from_20dma"] = _safe_pct(current_price, sum(closes[-20:]) / 20)
    if len(closes) >= 50:
        facts["dist_from_50dma"] = _safe_pct(current_price, sum(closes[-50:]) / 50)

    if highs and lows:
        atr20 = _atr(highs, lows, closes, period=20)
        latest_tr = _latest_true_range(highs, lows, closes)
        if atr20 and latest_tr is not None:
            facts["range_expansion"] = round(latest_tr / atr20, 2)
        if len(highs) >= 21:
            facts["crossed_level"] = _crossed_level(
                highs[-1], lows[-1], max(highs[-21:-1]), min(lows[-21:-1])
            )

    if volumes and len(volumes) >= 20 and sum(volumes[-20:]) > 0:
        avg_volume20 = sum(volumes[-20:]) / 20
        facts["volume_ratio"] = round(volumes[-1] / avg_volume20, 2) if avg_volume20 else None

    if len(closes) >= 31:
        daily_returns = [_pct_change(closes[i - 1], closes[i]) for i in range(1, len(closes))]
        latest_abs_return = abs(daily_returns[-1])
        facts["move_percentile"] = _move_percentile(daily_returns[:-1], latest_abs_return)

    return facts


def _pct_change(prior: float, current: float) -> float:
    return (current - prior) / prior if prior else 0.0


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


def _fetch_daily_history(ticker: str, period: str = "1y") -> Optional[dict]:
    """Raw daily OHLCV as plain lists, oldest-first. None if unavailable."""
    hist = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
    if hist.empty or len(hist) < 25:
        return None
    return {
        "closes":  [float(v) for v in hist["Close"]],
        "highs":   [float(v) for v in hist["High"]],
        "lows":    [float(v) for v in hist["Low"]],
        "volumes": [float(v) for v in hist["Volume"]],
    }


def _fetch_last_price(ticker: str, fallback: float) -> float:
    """Freshest available price (pre-market included). Falls back to yesterday's close on failure."""
    try:
        return float(yf.Ticker(ticker).fast_info.last_price)
    except Exception:
        return fallback


def _confirmation_instrument(label: str, ticker: str, tier: int) -> dict:
    try:
        history = _fetch_daily_history(ticker)
        if history is None:
            return {"label": label, "ticker": ticker, "tier": tier, "error": "insufficient history"}
        current = _fetch_last_price(ticker, fallback=history["closes"][-1])
        facts = _observation_facts_from_ohlcv(
            history["closes"], history["highs"], history["lows"], history["volumes"], current
        )
        return {"label": label, "ticker": ticker, "tier": tier, **facts}
    except Exception as exc:
        return {"label": label, "ticker": ticker, "tier": tier, "error": str(exc)}


def _credit_spread_proxy_instrument(tier: int = 3) -> dict:
    """HYG/LQD price ratio as a stand-in for high-yield vs. investment-grade spread direction."""
    hy_ticker, ig_ticker = CREDIT_SPREAD_PROXY
    label = f"Credit Spread Proxy ({hy_ticker}/{ig_ticker})"
    try:
        hy_hist = _fetch_daily_history(hy_ticker)
        ig_hist = _fetch_daily_history(ig_ticker)
        if hy_hist is None or ig_hist is None:
            return {"label": label, "ticker": f"{hy_ticker}/{ig_ticker}", "tier": tier, "error": "insufficient history"}
        n = min(len(hy_hist["closes"]), len(ig_hist["closes"]))
        ratio_closes = [hy_hist["closes"][-n:][i] / ig_hist["closes"][-n:][i] for i in range(n)]
        current_hy = _fetch_last_price(hy_ticker, fallback=hy_hist["closes"][-1])
        current_ig = _fetch_last_price(ig_ticker, fallback=ig_hist["closes"][-1])
        current_ratio = current_hy / current_ig
        # No single high/low/volume for a synthetic ratio — range/volume facts stay None.
        facts = _observation_facts_from_ohlcv(ratio_closes, None, None, None, current_ratio)
        return {"label": label, "ticker": f"{hy_ticker}/{ig_ticker}", "tier": tier, **facts}
    except Exception as exc:
        return {"label": label, "ticker": f"{hy_ticker}/{ig_ticker}", "tier": tier, "error": str(exc)}


def fetch_confirmation_observations() -> list[dict]:
    """Observation layer for the confirmation hierarchy (Tier 1 leadership, 2 index, 3 macro).

    Raw per-instrument facts only — no Quiet/Active/Stressed/Extreme status or
    Leading/Confirming/Diverging role classification yet (deferred, see PENDING TASKS).
    """
    results = []
    for tier, symbols in CONFIRMATION_TIERS.items():
        for label, ticker in symbols.items():
            results.append(_confirmation_instrument(label, ticker, tier))
    results.append(_credit_spread_proxy_instrument())
    return results


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


def fetch_watch_levels(key_levels: dict) -> list[dict]:
    """
    Fetch prices for tickers in key_levels['watch'] and compute distance
    to support/resistance.
    """
    watch_config = key_levels.get("watch", {})
    if not watch_config:
        return []

    # Use ticker keys directly as yfinance symbols — no hidden mapping.
    # Users enter the yfinance format in key_levels.yml (e.g. NQ=F, GC=F, ^VIX, DX-Y.NYB).
    symbols = {k: k for k in watch_config}
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


def load_key_levels_from_db() -> dict:
    """
    Load key_levels dict from DB. Returns same structure as the old YAML:
      {"positions": {ticker: {stop, note}}, "watch": {ticker: {support, resistance, ...}}}

    On first run (empty table), seeds from key_levels.yml if it exists.
    Falls back to empty dict if DB is unavailable.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from etrade_sync.db import get_connection
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT section, ticker, stop, support, resistance, alert_above, note FROM key_levels ORDER BY section, ticker")
            rows = cur.fetchall()

        # If table empty, seed from YAML if available
        if not rows:
            yml_path = Path(__file__).parent / "key_levels.yml"
            if yml_path.exists():
                import yaml
                with open(yml_path) as f:
                    data = yaml.safe_load(f) or {}
                save_key_levels_to_db(data)
                conn.close()
                return data
            conn.close()
            return {"positions": {}, "watch": {}}

        conn.close()
        result: dict = {"positions": {}, "watch": {}}
        for section, ticker, stop, support, resistance, alert_above, note in rows:
            entry = {}
            if stop        is not None: entry["stop"]        = float(stop)
            if support     is not None: entry["support"]     = float(support)
            if resistance  is not None: entry["resistance"]  = float(resistance)
            if alert_above is not None: entry["alert_above"] = float(alert_above)
            if note:                    entry["note"]         = note
            result[section][ticker] = entry
        return result
    except Exception:
        return {"positions": {}, "watch": {}}


def save_key_levels_to_db(key_levels: dict) -> None:
    """Upsert key_levels dict to DB. Deletes rows not in the new dict."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from etrade_sync.db import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Delete all existing rows then re-insert (clean state)
            cur.execute("DELETE FROM key_levels")
            for section in ("positions", "watch"):
                for ticker, vals in (key_levels.get(section) or {}).items():
                    cur.execute("""
                        INSERT INTO key_levels (section, ticker, stop, support, resistance, alert_above, note, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    """, (
                        section, ticker,
                        vals.get("stop"),
                        vals.get("support"),
                        vals.get("resistance"),
                        vals.get("alert_above"),
                        vals.get("note"),
                    ))
        conn.commit()
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
