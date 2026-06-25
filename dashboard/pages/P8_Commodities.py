import sys
from pathlib import Path
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from etrade_sync.db import get_connection

st.set_page_config(page_title="Commodities — Fifth Dragon Capital", layout="wide")
st.title("Commodities")

GROUPS = {
    "Precious Metals Futures": [
        ("Gold",      "GC=F"),
        ("Silver",    "SI=F"),
        ("Platinum",  "PL=F"),
        ("Palladium", "PA=F"),
    ],
    "Energy Futures": [
        ("Crude Oil (WTI)", "CL=F"),
        ("Brent Crude",     "BZ=F"),
        ("Natural Gas",     "NG=F"),
        ("RBOB Gasoline",   "RB=F"),
        ("Heating Oil",     "HO=F"),
        ("USO",             "USO"),
        ("UNG",             "UNG"),
        ("XLE",             "XLE"),
        ("CNQ",             "CNQ"),
    ],
    "Metals & Miners": [
        ("GLD",  "GLD"),
        ("SLV",  "SLV"),
        ("GDX",  "GDX"),
        ("GDXJ", "GDXJ"),
        ("SIL",  "SIL"),
        ("SILJ", "SILJ"),
        ("WPM",  "WPM"),
        ("FNV",  "FNV"),
        ("AEM",  "AEM"),
        ("RGLD", "RGLD"),
        ("GROY", "GROY"),
        ("PPLT", "PPLT"),
        ("SBSW", "SBSW"),
    ],
    "Uranium": [
        ("SRUUF (Spot Proxy)", "SRUUF"),
        ("CCJ",  "CCJ"),
        ("UEC",  "UEC"),
        ("URA",  "URA"),
        ("URNJ", "URNJ"),
        ("UROY", "UROY"),
        ("URNM", "URNM"),
        ("USAR", "USAR"),
        ("UUUU", "UUUU"),
    ],
    "Copper": [
        ("Copper Futures", "HG=F"),
        ("COPP", "COPP"),
        ("COPX", "COPX"),
        ("FCX",  "FCX"),
        ("TGB",  "TGB"),
        ("SCCO", "SCCO"),
    ],
    "Agriculture": [
        ("Corn",         "ZC=F"),
        ("Wheat",        "ZW=F"),
        ("Soybeans",     "ZS=F"),
        ("CORN ETF",     "CORN"),
        ("WEAT ETF",     "WEAT"),
        ("SOYB ETF",     "SOYB"),
    ],
}

COLS = 2


@st.cache_data(ttl=60)
def _load_alerts() -> dict[str, list[dict]]:
    """Returns {ticker: [{"condition", "threshold", "label"}, ...]} for enabled alerts."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ticker, condition, threshold::float, label "
                    "FROM price_alerts WHERE enabled = TRUE ORDER BY ticker, threshold"
                )
                rows = cur.fetchall()
    except Exception:
        return {}
    result: dict[str, list[dict]] = {}
    for ticker, condition, threshold, label in rows:
        result.setdefault(ticker, []).append(
            {"condition": condition, "threshold": threshold, "label": label or ""}
        )
    return result


# ── period / interval config ───────────────────────────────────────────────────

PERIODS = {
    "Intraday": ("1d",  "5m",  "%H:%M"),
    "5 Days":   ("5d",  "15m", "%m/%d %H:%M"),
    "1 Month":  ("1mo", "1d",  "%b %d"),
    "3 Months": ("3mo", "1d",  "%b %d"),
    "6 Months": ("6mo", "1d",  "%b '%y"),
}


@st.cache_data(ttl=300)
def fetch_ticker(ticker: str, period: str, interval: str) -> tuple[pd.DataFrame, float | None]:
    """Returns (bars_df, prev_close). One yfinance call per ticker+period."""
    df = yf.Ticker(ticker).history(period=period, interval=interval)
    if df.empty:
        return pd.DataFrame(), None
    df = df.reset_index()
    # Normalize datetime column name (yfinance returns 'Datetime' for intraday, 'Date' for daily)
    time_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={time_col: "Datetime"})
    df["Datetime"] = pd.to_datetime(df["Datetime"]).dt.tz_localize(None)
    df = df[["Datetime", "Open", "High", "Low", "Close", "Volume"]].copy()
    prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else None
    return df, prev_close


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


def _chart(label: str, ticker: str, df: pd.DataFrame, prev_close: float | None, x_fmt: str, alerts: list[dict] | None = None, show_ema: bool = False) -> None:
    if df.empty:
        st.caption(f"**{label}** ({ticker}) — no data")
        return

    price = float(df["Close"].iloc[-1])

    if prev_close and prev_close > 0:
        chg     = price - prev_close
        chg_pct = chg / prev_close * 100
        sign    = "+" if chg >= 0 else ""
        line_color = "#4CAF50" if chg >= 0 else "#ef5350"
        subtitle   = f"{price:,.3f}   {sign}{chg:,.3f} ({sign}{chg_pct:.2f}%)"
    else:
        line_color = "#888888"
        subtitle   = f"{price:,.3f}"

    df["color"] = (df["Close"] >= df["Open"]).map({True: "up", False: "down"})

    _color_scale = alt.Color(
        "color:N",
        scale=alt.Scale(domain=["up", "down"], range=["#4CAF50", "#ef5350"]),
        legend=None,
    )
    _x = alt.X("Datetime:T", title=None,
                axis=alt.Axis(format=x_fmt, labelAngle=-45, tickCount=6),
                scale=alt.Scale(padding=10))
    _tooltip = [
        alt.Tooltip("Datetime:T", title="Time",   format=x_fmt),
        alt.Tooltip("Open:Q",     title="Open",   format=",.3f"),
        alt.Tooltip("High:Q",     title="High",   format=",.3f"),
        alt.Tooltip("Low:Q",      title="Low",    format=",.3f"),
        alt.Tooltip("Close:Q",    title="Close",  format=",.3f"),
    ]

    base = alt.Chart(df).encode(x=_x, color=_color_scale, tooltip=_tooltip)

    wicks = base.mark_rule(strokeWidth=1).encode(
        y=alt.Y("Low:Q",  scale=alt.Scale(zero=False), title=None,
                axis=alt.Axis(format=",.2f")),
        y2=alt.Y2("High:Q"),
    )
    candles = base.mark_bar(size=5, stroke=None, opacity=1.0).encode(
        y=alt.Y("Open:Q",  scale=alt.Scale(zero=False), title=None,
                axis=alt.Axis(format=",.2f")),
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
        alt.Chart(df)
        .mark_bar(size=5, stroke=None, opacity=0.7)
        .encode(
            x=alt.X("Datetime:T", title=None,
                    axis=alt.Axis(format=x_fmt, labelAngle=-45, tickCount=6),
                    scale=alt.Scale(padding=10)),
            y=alt.Y("Volume:Q", title=None,
                    axis=alt.Axis(format="~s", tickCount=3)),
            color=_color_scale,
            tooltip=[
                alt.Tooltip("Datetime:T", title="Time",   format=x_fmt),
                alt.Tooltip("Volume:Q",   title="Volume", format=","),
            ],
        )
        .properties(height=60, width="container")
    )

    layers = [wicks, candles]

    if show_ema:
        n = len(df)
        if n >= 30:
            df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
            layers.append(
                alt.Chart(df).mark_line(color="#42A5F5", strokeWidth=1.2, opacity=0.85).encode(
                    x=alt.X("Datetime:T"),
                    y=alt.Y("EMA50:Q", scale=alt.Scale(zero=False)),
                    tooltip=[alt.Tooltip("EMA50:Q", title="50 EMA", format=",.2f")],
                )
            )
        if n >= 100:
            df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
            layers.append(
                alt.Chart(df).mark_line(color="#FFD54F", strokeWidth=1.2, opacity=0.85).encode(
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
                    scale=alt.Scale(domain=["above", "below"], range=["#FFA726", "#EF5350"]),
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

    rsi = _rsi_panel(df, x_fmt)
    panels = [candle_chart, volume_chart] + ([rsi] if rsi is not None else [])
    chart = alt.vconcat(*panels, spacing=4).resolve_scale(x="shared")
    st.altair_chart(chart, use_container_width=True)


# ── sidebar ────────────────────────────────────────────────────────────────────

st.sidebar.markdown("**Chart Period**")
period_choice = st.sidebar.selectbox("Period", list(PERIODS.keys()), index=4)
yf_period, yf_interval, x_fmt = PERIODS[period_choice]

if yf_period in ("1mo", "3mo", "6mo"):
    st.sidebar.markdown(
        "**Overlays**\n"
        "- 🔵 50 EMA\n"
        "- 🟡 200 EMA *(6M only)*\n"
        "- 🟠 Alert above\n"
        "- 🔴 Alert below\n"
        "- 🟣 RSI (30/70 bands)"
    )

INTERVALS = {"Off": None, "5 min": "5m", "10 min": "10m", "15 min": "15m"}
st.sidebar.markdown("**Auto-Refresh**")
refresh_choice = st.sidebar.selectbox(
    "Interval", list(INTERVALS.keys()),
    index=0 if period_choice != "Intraday" else 2,
    disabled=(period_choice != "Intraday"),
    help="Auto-refresh only applies to Intraday charts",
)
run_every = INTERVALS[refresh_choice] if period_choice == "Intraday" else None

if "last_period_p8" not in st.session_state:
    st.session_state.last_period_p8 = period_choice
if st.session_state.last_period_p8 != period_choice:
    st.cache_data.clear()
    st.session_state.last_period_p8 = period_choice


# ── commodity panel (auto-refreshes for intraday) ─────────────────────────────

@st.fragment(run_every=run_every)
def commodity_panel() -> None:
    st.caption(
        f"Last updated: {datetime.now().strftime('%H:%M:%S')}  •  "
        f"Period: {period_choice}  •  "
        + ("Data delayed ~15 min" if period_choice == "Intraday" else "Daily OHLC bars")
    )
    if st.button("↺ Refresh now", key="comm_refresh"):
        st.cache_data.clear()
        st.rerun(scope="fragment")

    all_alerts = _load_alerts()
    show_ema = yf_period in ("1mo", "3mo", "6mo")
    for group_name, tickers in GROUPS.items():
        st.subheader(group_name)
        for row_start in range(0, len(tickers), COLS):
            row  = tickers[row_start : row_start + COLS]
            cols = st.columns(COLS, gap="large")
            for col, (label, ticker) in zip(cols, row):
                with col:
                    df, prev_close = fetch_ticker(ticker, yf_period, yf_interval)
                    _chart(label, ticker, df, prev_close, x_fmt, all_alerts.get(ticker), show_ema)
        st.divider()


commodity_panel()
