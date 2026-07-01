import subprocess
import sys
from pathlib import Path
from datetime import datetime

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import query, execute

st.set_page_config(page_title="Market Monitor — Fifth Dragon Capital", layout="wide")
st.title("Market Monitor")


# ── Price Alerts ───────────────────────────────────────────────────────────────

st.header("Price Alerts")
st.caption("Alerts fire once when the threshold is crossed, then re-arm when price moves back. Run `python -m alerts.poller` to start the background poller.")

alerts = query("SELECT id, ticker, label, condition, threshold::float, enabled, triggered, last_fired_at FROM price_alerts ORDER BY id")

if alerts:
    def _status(a):
        if not a["enabled"]:
            return "⚫ Disabled"
        if a["triggered"]:
            return "🔴 Triggered"
        return "🟢 Armed"

    df = pd.DataFrame([{
        "ID":         a["id"],
        "Ticker":     a["ticker"],
        "Label":      a["label"] or "—",
        "Condition":  f"{'>' if a['condition'] == 'above' else '<'} {a['threshold']:,.2f}",
        "Status":     _status(a),
        "Last Fired": a["last_fired_at"].strftime("%Y-%m-%d %H:%M") if a["last_fired_at"] else "—",
    } for a in alerts])
    st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.info("No alerts defined yet. Add one below.")

st.divider()

col_add, col_manage = st.columns(2)

with col_add:
    st.subheader("Add Alert")
    with st.form("add_alert_form"):
        a1, a2 = st.columns(2)
        ticker_in    = a1.text_input("Ticker", placeholder="e.g. ^VIX, VIXY, NQ=F")
        label_in     = a2.text_input("Label (optional)", placeholder="e.g. VIX Fear Spike")
        a3, a4 = st.columns(2)
        condition_in = a3.selectbox("Condition", ["above", "below"])
        threshold_in = a4.number_input("Threshold", min_value=0.0, step=0.01, format="%.2f")
        if st.form_submit_button("Add Alert", type="primary"):
            if not ticker_in.strip():
                st.error("Ticker is required.")
            else:
                execute("""
                    INSERT INTO price_alerts (ticker, label, condition, threshold)
                    VALUES (%s, %s, %s, %s)
                """, (ticker_in.strip().upper(), label_in.strip() or None, condition_in, threshold_in))
                st.success(f"Alert added: {ticker_in.strip().upper()} {condition_in} {threshold_in:,.2f}")
                st.rerun()

with col_manage:
    st.subheader("Manage Alerts")
    if alerts:
        options = {
            f"#{a['id']} {a['ticker']} {'>' if a['condition'] == 'above' else '<'} {a['threshold']:,.2f}  {a['label'] or ''}".strip(): a
            for a in alerts
        }
        chosen_label = st.selectbox("Select alert", list(options.keys()))
        chosen = options[chosen_label]

        st.caption(f"Current threshold: **{chosen['threshold']:,.2f}**")
        new_threshold = st.number_input(
            "New threshold (leave 0 to keep current)", value=0.0,
            min_value=0.0, step=0.01, format="%.2f", key=f"manage_threshold_{chosen['id']}"
        )
        m1, m2, m3, m4 = st.columns(4)
        if m1.button("Save", use_container_width=True, type="primary"):
            if new_threshold > 0:
                execute("UPDATE price_alerts SET threshold = %s, triggered = FALSE WHERE id = %s",
                        (new_threshold, chosen["id"]))
                st.success(f"Threshold updated to {new_threshold:,.2f} and re-armed.")
            else:
                st.warning("Enter a value above 0 to change the threshold.")
            st.rerun()
        if m2.button("Enable" if not chosen["enabled"] else "Disable", use_container_width=True):
            execute("UPDATE price_alerts SET enabled = %s WHERE id = %s", (not chosen["enabled"], chosen["id"]))
            st.rerun()
        if m3.button("Re-arm", use_container_width=True, help="Reset triggered state so alert can fire again"):
            execute("UPDATE price_alerts SET triggered = FALSE WHERE id = %s", (chosen["id"],))
            st.success("Alert re-armed.")
            st.rerun()
        if m4.button("Delete", use_container_width=True, type="secondary"):
            execute("DELETE FROM price_alerts WHERE id = %s", (chosen["id"],))
            st.success("Alert deleted.")
            st.rerun()
    else:
        st.caption("No alerts to manage.")

st.divider()


GROUPS = {
    "US Indices": [
        ("S&P 500",        "^GSPC"),
        ("Dow Jones",      "^DJI"),
        ("NASDAQ",         "^IXIC"),
        ("Russell 2000",   "^RUT"),
        ("NYSE Composite", "^NYA"),
        ("DXY (US Dollar)", "DX-Y.NYB"),
    ],
    "Global Indices": [
        ("FTSE 100",   "^FTSE"),
        ("DAX",        "^GDAXI"),
        ("Nikkei 225", "^N225"),
        ("Hang Seng",  "^HSI"),
        ("Shanghai",   "000001.SS"),
    ],
    "ETFs": [
        ("SMH",  "SMH"),
        ("QQQ",  "QQQ"),
        ("SPY",  "SPY"),
        ("IWM",  "IWM"),
        ("RSP",  "RSP"),
        ("TWM",  "TWM"),
        ("TLT",  "TLT"),
        ("TBT",  "TBT"),
        ("UVXY", "UVXY"),
        ("VIXY", "VIXY"),
    ],
    "Volatility, Rates & Bond Futures": [
        ("VIX",           "^VIX"),
        ("VVIX",          "^VVIX"),
        ("10Y Yield",     "^TNX"),
        ("10Y Note Fut.", "ZN=F"),
        ("30Y Bond Fut.", "ZB=F"),
    ],
    "Defensive Sectors": [
        ("VHT Healthcare", "VHT"),
        ("XLV Healthcare", "XLV"),
        ("XLP Cons. Staples", "XLP"),
        ("XLU Utilities",  "XLU"),
    ],
}

COLS = 2  # charts per row


PERIODS = {
    "Intraday": ("2d",  "5m",  "%H:%M",       "%b %d %H:%M"),
    "5 Days":   ("5d",  "15m", "%m/%d %H:%M", "%b %d %H:%M"),
    "1 Month":  ("1mo", "1d",  "%b %d",       "%b %d, %Y"),
    "3 Months": ("3mo", "1d",  "%b %d",       "%b %d, %Y"),
    "6 Months": ("6mo", "1d",  "%b '%y",      "%b %d, %Y"),
}


@st.cache_data(ttl=300)
def fetch_ticker(ticker: str, period: str = "2d", interval: str = "5m") -> tuple[pd.DataFrame, float | None]:
    """Returns (bars_df, prev_close). One yfinance call per ticker+period."""
    df = yf.Ticker(ticker).history(period=period, interval=interval)
    if df.empty:
        return pd.DataFrame(), None
    df = df.reset_index()
    time_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={time_col: "Datetime"})
    df["Datetime"] = pd.to_datetime(df["Datetime"]).dt.tz_localize(None)
    df = df[["Datetime", "Open", "High", "Low", "Close", "Volume"]].copy()
    prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else None
    # Intraday only: filter to today, use yesterday's close as prev_close
    if period == "2d":
        today = df["Datetime"].dt.date.max()
        prev_closes = df[df["Datetime"].dt.date < today]["Close"]
        prev_close  = float(prev_closes.iloc[-1]) if not prev_closes.empty else None
        df = df[df["Datetime"].dt.date == today].copy()
    return df, prev_close


def _compute_adx(df: pd.DataFrame, period: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (adx, pdi, ndi) arrays. Warmup = period*2 bars."""
    n = len(df)
    high  = df["High"].values
    low   = df["Low"].values
    close = df["Close"].values

    tr = np.zeros(n); pdm = np.zeros(n); ndm = np.zeros(n)
    for i in range(1, n):
        tr[i]  = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
        up = high[i] - high[i-1]
        dn = low[i-1] - low[i]
        pdm[i] = up if (up > dn and up > 0) else 0
        ndm[i] = dn if (dn > up and dn > 0) else 0

    def _wilder(arr):
        out = np.zeros(n)
        out[period] = arr[1:period+1].sum()
        for i in range(period+1, n):
            out[i] = out[i-1] - out[i-1] / period + arr[i]
        return out

    atr14 = _wilder(tr); pdm14 = _wilder(pdm); ndm14 = _wilder(ndm)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = np.where(atr14 > 0, 100 * pdm14 / atr14, 0.0)
        ndi = np.where(atr14 > 0, 100 * ndm14 / atr14, 0.0)
        dx  = np.where((pdi + ndi) > 0, 100 * np.abs(pdi - ndi) / (pdi + ndi), 0.0)

    adx = np.zeros(n)
    start = period * 2
    if start < n:
        adx[start] = dx[period:start+1].mean()
        for i in range(start+1, n):
            adx[i] = (adx[i-1] * (period-1) + dx[i]) / period
    adx[:start] = np.nan; pdi[:period] = np.nan; ndi[:period] = np.nan
    return adx, pdi, ndi


def _compute_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> tuple[np.ndarray, np.ndarray]:
    n = len(df)
    high  = df["High"].values
    low   = df["Low"].values
    close = df["Close"].values

    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))

    atr = np.zeros(n)
    atr[period - 1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period

    hl2 = (high + low) / 2
    bu = hl2 + multiplier * atr
    bl = hl2 - multiplier * atr

    fu = bu.copy()
    fl = bl.copy()
    st_vals  = np.full(n, np.nan)
    st_dir   = np.full(n, "", dtype=object)
    st_dir[0] = "bull"
    st_vals[0] = fl[0]

    for i in range(1, n):
        fu[i] = bu[i] if bu[i] < fu[i-1] or close[i-1] > fu[i-1] else fu[i-1]
        fl[i] = bl[i] if bl[i] > fl[i-1] or close[i-1] < fl[i-1] else fl[i-1]
        if close[i] > fu[i-1]:
            st_dir[i] = "bull"
        elif close[i] < fl[i-1]:
            st_dir[i] = "bear"
        else:
            st_dir[i] = st_dir[i-1]
        st_vals[i] = fl[i] if st_dir[i] == "bull" else fu[i]

    return st_vals, st_dir


def _signal_summary(df: pd.DataFrame, show_ema: bool, show_st: bool, indicator: str) -> str:
    if df.empty or len(df) < 2:
        return ""
    close = df["Close"]
    last  = float(close.iloc[-1])
    lines = []

    # ── Trend ──
    trend_rows = []
    if show_ema and len(df) >= 30:
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        arrow = "▲" if last > ema50 else "▼"
        trend_rows.append(f"50 EMA   {arrow}  ({ema50:,.2f})")
    if show_ema and len(df) >= 100:
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        arrow  = "▲" if last > ema200 else "▼"
        trend_rows.append(f"200 EMA  {arrow}  ({ema200:,.2f})")
    if show_st and len(df) > 11:
        st_vals, st_dir = _compute_supertrend(df)
        arrow = "▲" if st_dir[-1] == "bull" else "▼"
        trend_rows.append(f"Supertrend  {arrow}  ({st_vals[-1]:,.2f})")
    if trend_rows:
        lines += ["**Trend**", "──────────────"] + trend_rows + [""]

    # ── Momentum ──
    mom_rows = []
    if indicator == "StochRSI" and len(df) >= 28:
        d    = close.diff()
        gain = d.clip(lower=0).rolling(14).mean()
        loss = (-d.clip(upper=0)).rolling(14).mean()
        rsi  = 100 - (100 / (1 + gain / loss))
        stoch = (rsi - rsi.rolling(14).min()) / (rsi.rolling(14).max() - rsi.rolling(14).min()) * 100
        k = stoch.rolling(3).mean().iloc[-1]
        if not pd.isna(k):
            zone = "Oversold" if k < 20 else "Overbought" if k > 80 else "Neutral"
            mom_rows += [f"StochRSI %K  {k:.0f}", zone]
    elif indicator == "RSI" and len(df) >= 15:
        d    = close.diff()
        gain = d.clip(lower=0).rolling(14).mean()
        loss = (-d.clip(upper=0)).rolling(14).mean()
        rsi_val = float((100 - (100 / (1 + gain / loss))).iloc[-1])
        if not pd.isna(rsi_val):
            zone = "Oversold" if rsi_val < 30 else "Overbought" if rsi_val > 70 else "Neutral"
            mom_rows += [f"RSI  {rsi_val:.0f}", zone]
    elif indicator == "MACD" and len(df) >= 27:
        ema12  = close.ewm(span=12, adjust=False).mean()
        ema26  = close.ewm(span=26, adjust=False).mean()
        macd   = float((ema12 - ema26).iloc[-1])
        signal = float((ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1])
        bias   = "Bullish" if macd > signal else "Bearish"
        mom_rows += [f"MACD  {macd:.3f}", f"Signal  {signal:.3f}", bias]
    elif indicator == "ADX" and len(df) >= 29:
        adx, pdi, ndi = _compute_adx(df)
        adx_val = float(adx[-1]); pdi_val = float(pdi[-1]); ndi_val = float(ndi[-1])
        if not np.isnan(adx_val):
            strength  = "Strong" if adx_val > 50 else "Trending" if adx_val > 25 else "Developing" if adx_val > 20 else "Weak / Ranging"
            direction = "Bullish (+DI > -DI)" if pdi_val > ndi_val else "Bearish (-DI > +DI)"
            mom_rows += [f"ADX  {adx_val:.1f}  —  {strength}",
                         f"+DI {pdi_val:.1f}   -DI {ndi_val:.1f}", direction]
    if mom_rows:
        lines += ["**Momentum**", "──────────────"] + mom_rows + [""]

    # ── Volume ──
    if "Volume" in df.columns and len(df) >= 20:
        last_vol = float(df["Volume"].iloc[-1])
        avg_vol  = float(df["Volume"].rolling(20).mean().iloc[-1])
        if avg_vol > 0:
            ratio = last_vol / avg_vol
            label = "High" if ratio > 1.5 else "Low" if ratio < 0.5 else "Average"
            lines += ["**Volume**", "──────────────", f"{label}  ({ratio:.1f}× avg)", ""]

    # ── Risk ──
    if len(df) >= 15:
        high = df["High"]; low = df["Low"]
        tr  = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.ewm(span=14, adjust=False).mean().iloc[-1])
        if not pd.isna(atr):
            atr_pct = atr / last * 100
            lines += ["**Risk**", "──────────────", f"ATR(14)  {atr:,.2f}  ({atr_pct:.1f}%)"]

    return "\n\n".join(lines)


def _adx_panel(df: pd.DataFrame, x_fmt: str) -> alt.Chart | None:
    if len(df) < 29:
        return None
    adx, pdi, ndi = _compute_adx(df)
    adx_df = df[["Datetime"]].copy()
    adx_df["ADX"] = adx
    adx_df["PDI"] = pdi
    adx_df["NDI"] = ndi

    ref = pd.DataFrame({"level": [25]})
    _x  = alt.X("Datetime:T", title=None,
                 axis=alt.Axis(format=x_fmt, labelAngle=-45, tickCount=6),
                 scale=alt.Scale(padding=10))
    _yscale = alt.Scale(zero=False)
    _yaxis  = alt.Axis(tickCount=4, title="ADX")

    adx_line = (alt.Chart(adx_df).mark_line(color="#BDBDBD", strokeWidth=2.0)
        .encode(x=_x, y=alt.Y("ADX:Q", scale=_yscale, axis=_yaxis),
                tooltip=[alt.Tooltip("Datetime:T", title="Time", format=x_fmt),
                         alt.Tooltip("ADX:Q", title="ADX", format=".1f")]))
    pdi_line = (alt.Chart(adx_df).mark_line(color="#4CAF50", strokeWidth=1.2)
        .encode(x=_x, y=alt.Y("PDI:Q", scale=_yscale),
                tooltip=[alt.Tooltip("PDI:Q", title="+DI", format=".1f")]))
    ndi_line = (alt.Chart(adx_df).mark_line(color="#ef5350", strokeWidth=1.2)
        .encode(x=_x, y=alt.Y("NDI:Q", scale=_yscale),
                tooltip=[alt.Tooltip("NDI:Q", title="-DI", format=".1f")]))
    threshold = (alt.Chart(ref).mark_rule(strokeDash=[4, 3], strokeWidth=1, opacity=0.6)
        .encode(y=alt.Y("level:Q"), color=alt.value("#FFA726")))

    return (adx_line + pdi_line + ndi_line + threshold).properties(height=80, width="container")


def _macd_panel(df: pd.DataFrame, x_fmt: str) -> alt.Chart | None:
    if len(df) < 27:
        return None
    macd_df = df[["Datetime"]].copy()
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    macd_df["MACD"]      = ema12 - ema26
    macd_df["Signal"]    = macd_df["MACD"].ewm(span=9, adjust=False).mean()
    macd_df["Histogram"] = macd_df["MACD"] - macd_df["Signal"]
    macd_df["hcolor"]    = (macd_df["Histogram"] >= 0).map({True: "pos", False: "neg"})

    _x = alt.X("Datetime:T", title=None,
                axis=alt.Axis(format=x_fmt, labelAngle=-45, tickCount=6),
                scale=alt.Scale(padding=10))
    hist = (
        alt.Chart(macd_df).mark_bar(size=3, opacity=0.7)
        .encode(
            x=_x,
            y=alt.Y("Histogram:Q", scale=alt.Scale(zero=True), title=None,
                    axis=alt.Axis(tickCount=3, title="MACD")),
            color=alt.Color("hcolor:N",
                            scale=alt.Scale(domain=["pos", "neg"], range=["#4CAF50", "#ef5350"]),
                            legend=None),
            tooltip=[
                alt.Tooltip("Datetime:T",  title="Time",      format=x_fmt),
                alt.Tooltip("Histogram:Q", title="Histogram", format=".3f"),
            ],
        )
    )
    macd_line = (
        alt.Chart(macd_df).mark_line(color="#42A5F5", strokeWidth=1.2)
        .encode(x=_x, y=alt.Y("MACD:Q",   scale=alt.Scale(zero=True)),
                tooltip=[alt.Tooltip("MACD:Q",   title="MACD",   format=".3f")])
    )
    signal_line = (
        alt.Chart(macd_df).mark_line(color="#FFA726", strokeWidth=1.2)
        .encode(x=_x, y=alt.Y("Signal:Q", scale=alt.Scale(zero=True)),
                tooltip=[alt.Tooltip("Signal:Q", title="Signal", format=".3f")])
    )
    zero = alt.Chart(pd.DataFrame({"z": [0]})).mark_rule(color="#888", strokeWidth=0.5, opacity=0.5).encode(y="z:Q")
    return (hist + macd_line + signal_line + zero).properties(height=80, width="container")


def _stochrsi_panel(df: pd.DataFrame, x_fmt: str) -> alt.Chart | None:
    if len(df) < 28:
        return None
    d = df["Close"].diff()
    gain = d.clip(lower=0).rolling(14).mean()
    loss = (-d.clip(upper=0)).rolling(14).mean()
    rsi = 100 - (100 / (1 + gain / loss))
    rsi_min = rsi.rolling(14).min()
    rsi_max = rsi.rolling(14).max()
    stoch = (rsi - rsi_min) / (rsi_max - rsi_min) * 100

    srsi_df = df[["Datetime"]].copy()
    srsi_df["%K"] = stoch.rolling(3).mean()
    srsi_df["%D"] = srsi_df["%K"].rolling(3).mean()

    zones = pd.DataFrame([{"y1": 0, "y2": 20, "z": "os"}, {"y1": 80, "y2": 100, "z": "ob"}])
    bands = pd.DataFrame({"level": [20, 80]})
    _x = alt.X("Datetime:T", title=None,
                axis=alt.Axis(format=x_fmt, labelAngle=-45, tickCount=6),
                scale=alt.Scale(padding=10))
    _y = alt.Scale(domain=[0, 100])

    zone_rects = (
        alt.Chart(zones).mark_rect(opacity=0.08)
        .encode(
            y=alt.Y("y1:Q", scale=_y),
            y2=alt.Y2("y2:Q"),
            color=alt.Color("z:N",
                scale=alt.Scale(domain=["os", "ob"], range=["#4CAF50", "#ef5350"]),
                legend=None,
            ),
        )
    )
    k_line = (
        alt.Chart(srsi_df).mark_line(color="#42A5F5", strokeWidth=1.2)
        .encode(
            x=_x,
            y=alt.Y("%K:Q", scale=_y, title=None,
                    axis=alt.Axis(values=[20, 80], tickCount=3, title="StochRSI")),
            tooltip=[
                alt.Tooltip("Datetime:T", title="Time", format=x_fmt),
                alt.Tooltip("%K:Q",       title="%K",   format=".1f"),
            ],
        )
    )
    d_line = (
        alt.Chart(srsi_df).mark_line(color="#FFA726", strokeWidth=1.2)
        .encode(
            x=_x,
            y=alt.Y("%D:Q", scale=_y),
            tooltip=[alt.Tooltip("%D:Q", title="%D", format=".1f")],
        )
    )
    band_rules = (
        alt.Chart(bands)
        .mark_rule(strokeDash=[3, 3], strokeWidth=1, opacity=0.5)
        .encode(
            y=alt.Y("level:Q"),
            color=alt.condition(
                alt.datum.level == 80,
                alt.value("#ef5350"),
                alt.value("#4CAF50"),
            ),
        )
    )
    return (zone_rects + k_line + d_line + band_rules).properties(height=70, width="container")


def _rsi_panel(df: pd.DataFrame, x_fmt: str) -> alt.Chart | None:
    if len(df) < 15:
        return None
    d = df["Close"].diff()
    gain = d.clip(lower=0).rolling(14).mean()
    loss = (-d.clip(upper=0)).rolling(14).mean()
    rsi_df = df[["Datetime"]].copy()
    rsi_df["RSI"] = 100 - (100 / (1 + gain / loss))

    bands = pd.DataFrame({"level": [30, 70]})
    rsi_line = (
        alt.Chart(rsi_df)
        .mark_line(color="#CE93D8", strokeWidth=1.2)
        .encode(
            x=alt.X("Datetime:T", title=None,
                    axis=alt.Axis(format=x_fmt, labelAngle=-45, tickCount=6),
                    scale=alt.Scale(padding=10)),
            y=alt.Y("RSI:Q", scale=alt.Scale(domain=[0, 100]), title=None,
                    axis=alt.Axis(values=[30, 70], tickCount=3, title="RSI")),
            tooltip=[
                alt.Tooltip("Datetime:T", title="Time", format=x_fmt),
                alt.Tooltip("RSI:Q",      title="RSI",  format=".1f"),
            ],
        )
    )
    band_rules = (
        alt.Chart(bands)
        .mark_rule(strokeDash=[3, 3], strokeWidth=1, opacity=0.5)
        .encode(
            y=alt.Y("level:Q"),
            color=alt.condition(
                alt.datum.level == 70,
                alt.value("#ef5350"),
                alt.value("#4CAF50"),
            ),
        )
    )
    return (rsi_line + band_rules).properties(height=70, width="container")


def _intraday_chart(label: str, ticker: str, today_df: pd.DataFrame, prev_close: float | None, fmt: str = "%H:%M", tooltip_fmt: str = "", alerts: list[dict] | None = None, show_ema: bool = False, indicator: str = "None", show_st: bool = False) -> None:
    if today_df.empty:
        st.caption(f"**{label}** ({ticker})  —  no data")
        return

    price = float(today_df["Close"].iloc[-1])

    if prev_close and prev_close > 0:
        chg     = price - prev_close
        chg_pct = chg / prev_close * 100
        sign    = "+" if chg >= 0 else ""
        line_color = "#4CAF50" if chg >= 0 else "#ef5350"
        subtitle = f"{price:,.2f}   {sign}{chg:,.2f} ({sign}{chg_pct:.2f}%)"
    else:
        line_color = "#888888"
        subtitle = f"{price:,.2f}"

    today_df["color"] = (today_df["Close"] >= today_df["Open"]).map({True: "up", False: "down"})

    base = alt.Chart(today_df).encode(
        x=alt.X("Datetime:T", title=None,
                axis=alt.Axis(format=fmt, labelAngle=-45, tickCount=6),
                scale=alt.Scale(padding=10)),
        color=alt.Color(
            "color:N",
            scale=alt.Scale(domain=["up", "down"], range=["#4CAF50", "#ef5350"]),
            legend=None,
        ),
        tooltip=[
            alt.Tooltip("Datetime:T", title="Time",  format=tooltip_fmt or fmt),
            alt.Tooltip("Open:Q",     title="Open",  format=",.2f"),
            alt.Tooltip("High:Q",     title="High",  format=",.2f"),
            alt.Tooltip("Low:Q",      title="Low",   format=",.2f"),
            alt.Tooltip("Close:Q",    title="Close", format=",.2f"),
        ],
    )

    wicks = base.mark_rule(strokeWidth=1).encode(
        y=alt.Y("Low:Q",  title=None, scale=alt.Scale(zero=False), axis=alt.Axis(format=",.2f")),
        y2=alt.Y2("High:Q"),
    )
    candles = base.mark_bar(size=5, stroke=None, opacity=1.0).encode(
        y=alt.Y("Open:Q",  scale=alt.Scale(zero=False), axis=alt.Axis(format=",.2f")),
        y2=alt.Y2("Close:Q"),
    )

    candle_chart = (wicks + candles).properties(
        title=alt.TitleParams(
            text=f"{label}  ({ticker})",
            subtitle=subtitle,
            fontSize=13,
            subtitleFontSize=11,
            subtitleColor=line_color,
        ),
        height=240,
        width="container",
    )

    volume_chart = (
        alt.Chart(today_df)
        .mark_bar(size=5, stroke=None, opacity=0.7)
        .encode(
            x=alt.X("Datetime:T", title=None, axis=alt.Axis(format=fmt, labelAngle=-45, tickCount=6)),
            y=alt.Y("Volume:Q",   title=None, axis=alt.Axis(format="~s", tickCount=3)),
            color=alt.Color(
                "color:N",
                scale=alt.Scale(domain=["up", "down"], range=["#4CAF50", "#ef5350"]),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("Datetime:T", title="Time",   format=fmt),
                alt.Tooltip("Volume:Q",   title="Volume", format=","),
            ],
        )
        .properties(height=60, width="container")
    )

    layers = [wicks, candles]

    if show_st and len(today_df) > 11:
        st_vals, st_dir = _compute_supertrend(today_df)
        st_df = today_df[["Datetime"]].copy()
        st_df["ST"] = st_vals
        st_df["dir"] = st_dir
        for color_val, color_hex in [("bull", "#4CAF50"), ("bear", "#ef5350")]:
            seg = st_df[st_df["dir"] == color_val].copy()
            if not seg.empty:
                layers.append(
                    alt.Chart(seg).mark_line(color=color_hex, strokeWidth=1.8, strokeDash=[])
                    .encode(
                        x=alt.X("Datetime:T"),
                        y=alt.Y("ST:Q", scale=alt.Scale(zero=False)),
                        tooltip=[
                            alt.Tooltip("Datetime:T", title="Time", format=fmt),
                            alt.Tooltip("ST:Q",       title="Supertrend", format=",.2f"),
                        ],
                    )
                )

    if show_ema:
        n = len(today_df)
        if n >= 30:
            today_df["EMA50"] = today_df["Close"].ewm(span=50, adjust=False).mean()
            layers.append(
                alt.Chart(today_df).mark_line(color="#42A5F5", strokeWidth=1.2, opacity=0.85).encode(
                    x=alt.X("Datetime:T"),
                    y=alt.Y("EMA50:Q", scale=alt.Scale(zero=False)),
                    tooltip=[alt.Tooltip("EMA50:Q", title="50 EMA", format=",.2f")],
                )
            )
        if n >= 100:
            today_df["EMA200"] = today_df["Close"].ewm(span=200, adjust=False).mean()
            layers.append(
                alt.Chart(today_df).mark_line(color="#FFD54F", strokeWidth=1.2, opacity=0.85).encode(
                    x=alt.X("Datetime:T"),
                    y=alt.Y("EMA200:Q", scale=alt.Scale(zero=False)),
                    tooltip=[alt.Tooltip("EMA200:Q", title="200 EMA", format=",.2f")],
                )
            )

    if alerts:
        alert_df = pd.DataFrame(alerts)
        layers.append(
            alt.Chart(alert_df)
            .mark_rule(strokeDash=[6, 3], strokeWidth=1.5)
            .encode(
                y=alt.Y("threshold:Q", scale=alt.Scale(zero=False)),
                color=alt.Color(
                    "condition:N",
                    scale=alt.Scale(domain=["above", "below"], range=["#AB47BC", "#FF7043"]),
                    legend=None,
                ),
                tooltip=[
                    alt.Tooltip("condition:N", title="Alert"),
                    alt.Tooltip("threshold:Q", title="Level", format=",.2f"),
                    alt.Tooltip("label:N",     title="Note"),
                ],
            )
        )

    if len(layers) > 2:
        candle_chart = alt.layer(*layers).properties(
            title=candle_chart.title, height=candle_chart.height, width=candle_chart.width
        )

    ind_panel = None
    if indicator == "RSI":
        ind_panel = _rsi_panel(today_df, fmt)
    elif indicator == "StochRSI":
        ind_panel = _stochrsi_panel(today_df, fmt)
    elif indicator == "MACD":
        ind_panel = _macd_panel(today_df, fmt)
    elif indicator == "ADX":
        ind_panel = _adx_panel(today_df, fmt)
    panels = [candle_chart, volume_chart] + ([ind_panel] if ind_panel is not None else [])
    chart = alt.vconcat(*panels, spacing=4).resolve_scale(x="shared")
    summary = _signal_summary(today_df, show_ema, show_st, indicator)
    if summary:
        with st.popover("📊", use_container_width=False):
            st.markdown(summary)

    st.altair_chart(chart, use_container_width=True)


# ── sidebar ────────────────────────────────────────────────────────────────────

st.sidebar.markdown("**Chart Period**")
period_choice = st.sidebar.selectbox("Period", list(PERIODS.keys()), index=4)
yf_period, yf_interval, x_fmt, tooltip_fmt = PERIODS[period_choice]

show_st = st.sidebar.checkbox(
    "Supertrend (ATR 10, ×3.0)", value=True,
    help="🟢 Green line below price = bullish trend. 🔴 Red line above price = bearish. Line flip = trend change signal. Also acts as a trailing stop level."
)

st.sidebar.markdown("**Indicator**")
indicator = st.sidebar.radio("Indicator", ["None", "RSI", "StochRSI", "MACD", "ADX"], index=2, label_visibility="collapsed")

with st.sidebar.popover("ⓘ How to read"):
    if show_st:
        st.markdown(
            "**Supertrend (ATR 10, ×3.0)**\n\n"
            "- 🟢 **Green line below price** = bullish trend — acts as trailing stop floor\n"
            "- 🔴 **Red line above price** = bearish trend — acts as resistance/stop ceiling\n"
            "- **Line flip** = trend change signal\n\n"
            "---"
        )
    if indicator == "RSI":
        st.markdown(
            "**RSI (14)** — momentum oscillator, 0–100\n\n"
            "- Below **30** → oversold, watch for reversal up\n"
            "- Above **70** → overbought, watch for reversal down\n"
            "- High-conviction when RSI diverges from price "
            "(price makes new high, RSI doesn't)"
        )
    elif indicator == "StochRSI":
        st.markdown(
            "**Stochastic RSI (14,14,3,3)** — faster than RSI, 0–100\n\n"
            "**Signals (crossovers in the bands are key):**\n"
            "- 🟢 **%K crosses above %D from below 20** → bullish, potential entry\n"
            "- 🔴 **%K crosses below %D from above 80** → bearish, potential exit\n\n"
            "Crossovers between 20–80 are weaker — wait for band extremes for "
            "high-conviction signals."
        )
    elif indicator == "MACD":
        st.markdown(
            "**MACD (12,26,9)**\n\n"
            "- 🔵 **MACD line** crosses above 🟠 **Signal** → bullish momentum\n"
            "- 🔵 **MACD line** crosses below 🟠 **Signal** → bearish momentum\n"
            "- **Histogram** growing green → strengthening uptrend\n"
            "- **Histogram** growing red → strengthening downtrend\n"
            "- Crossovers near the zero line carry more weight"
        )
    elif indicator == "ADX":
        st.markdown(
            "**ADX (14)** — trend *strength*, not direction\n\n"
            "- ADX < 20 → weak / ranging — other signals unreliable\n"
            "- ADX 20–25 → trend developing\n"
            "- ADX > 25 → trending — directional trades valid\n"
            "- ADX > 50 → strong trend — don't fade it\n\n"
            "**Direction (+DI / -DI lines):**\n"
            "- 🟢 **+DI above -DI** → bullish pressure\n"
            "- 🔴 **-DI above +DI** → bearish pressure\n"
            "- +DI crosses above -DI **while ADX > 25** → high-conviction entry"
        )
    else:
        st.markdown("Select an indicator to see how to read it.")

overlay_lines = ["- 🟢 Supertrend bull  🔴 bear"] if show_st else []
if yf_period in ("1mo", "3mo", "6mo"):
    overlay_lines += ["- 🔵 50 EMA", "- 🟡 200 EMA *(6M only)*"]
overlay_lines += ["- 🟣 Alert above", "- 🟤 Alert below"]
if overlay_lines:
    st.sidebar.markdown("**Overlays**\n" + "\n".join(overlay_lines))
if indicator == "RSI":
    st.sidebar.markdown("- 🟣 RSI (30/70 bands)")
elif indicator == "StochRSI":
    st.sidebar.markdown("- 🔵 %K  🟠 %D  (20/80 bands)")
elif indicator == "MACD":
    st.sidebar.markdown("- 🔵 MACD  🟠 Signal  🟩🟥 Histogram")
elif indicator == "ADX":
    st.sidebar.markdown("- ⬜ ADX  🟢 +DI  🔴 -DI  (25 = trend threshold)")

st.sidebar.markdown("**Auto-Refresh**")
INTERVALS = {"Off": None, "5 min": "5m", "10 min": "10m", "15 min": "15m"}
choice    = st.sidebar.selectbox(
    "Interval", list(INTERVALS.keys()), index=2,
    disabled=(period_choice != "Intraday"),
    help="Auto-refresh only applies to Intraday",
)
run_every = INTERVALS[choice] if period_choice == "Intraday" else None

# Clear cache automatically when period changes
if "last_period" not in st.session_state:
    st.session_state.last_period = period_choice
if st.session_state.last_period != period_choice:
    st.cache_data.clear()
    st.session_state.last_period = period_choice

st.sidebar.divider()
st.sidebar.markdown("**Price Alerts**")
if st.sidebar.button("▶ Run Alert Poll", use_container_width=True, type="primary"):
    venv_python  = sys.executable
    project_root = str(Path(__file__).parent.parent.parent)
    with st.sidebar:
        with st.spinner("Polling…"):
            result = subprocess.run(
                [venv_python, "-m", "alerts.poller", "--once"],
                capture_output=True, text=True, cwd=project_root,
            )
    output = result.stdout + (result.stderr if result.returncode != 0 else "")
    st.sidebar.code(output.strip() or "No output.", language=None)
    st.rerun()

# ── market data fragment (auto-refreshes independently) ───────────────────────

@st.fragment(run_every=run_every)
def market_panel() -> None:
    period_label = "Intraday 5-min bars" if yf_interval in ("5m", "15m") else f"{period_choice} daily bars"
    st.caption(
        f"Last updated: {datetime.now().strftime('%H:%M:%S')}  •  "
        f"Data delayed ~15 min  •  {period_label}"
    )
    if st.button("↺ Refresh now", key="manual_refresh"):
        st.cache_data.clear()
        st.rerun(scope="fragment")

    alerts_by_ticker: dict[str, list[dict]] = {}
    for a in (alerts or []):
        if a["enabled"]:
            alerts_by_ticker.setdefault(a["ticker"], []).append(
                {"condition": a["condition"], "threshold": a["threshold"], "label": a["label"] or ""}
            )

    show_ema = yf_period in ("1mo", "3mo", "6mo")
    for group_name, tickers in GROUPS.items():
        st.subheader(group_name)
        for row_start in range(0, len(tickers), COLS):
            row  = tickers[row_start : row_start + COLS]
            cols = st.columns(COLS, gap="large")
            for col, (label, ticker) in zip(cols, row):
                with col:
                    df, prev_close = fetch_ticker(ticker, yf_period, yf_interval)
                    _intraday_chart(label, ticker, df, prev_close, x_fmt, tooltip_fmt, alerts_by_ticker.get(ticker), show_ema, indicator, show_st)
        st.divider()


market_panel()
