import sys
from pathlib import Path
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

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
    ],
    "Metals & Miners": [
        ("GLD",  "GLD"),
        ("SLV",  "SLV"),
        ("GDX",  "GDX"),
        ("GDXJ", "GDXJ"),
        ("USO",  "USO"),
        ("UNG",  "UNG"),
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

COLS = 3

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


def _chart(label: str, ticker: str, df: pd.DataFrame, prev_close: float | None, x_fmt: str) -> None:
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
                axis=alt.Axis(format=x_fmt, labelAngle=-45, tickCount=6))
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
    )

    volume_chart = (
        alt.Chart(df)
        .mark_bar(size=5, stroke=None, opacity=0.7)
        .encode(
            x=alt.X("Datetime:T", title=None,
                    axis=alt.Axis(format=x_fmt, labelAngle=-45, tickCount=6)),
            y=alt.Y("Volume:Q", title=None,
                    axis=alt.Axis(format="~s", tickCount=3)),
            color=_color_scale,
            tooltip=[
                alt.Tooltip("Datetime:T", title="Time",   format=x_fmt),
                alt.Tooltip("Volume:Q",   title="Volume", format=","),
            ],
        )
        .properties(height=60)
    )

    chart = alt.vconcat(candle_chart, volume_chart, spacing=4).resolve_scale(x="shared")
    st.altair_chart(chart, use_container_width=True)


# ── sidebar ────────────────────────────────────────────────────────────────────

st.sidebar.markdown("**Chart Period**")
period_choice = st.sidebar.selectbox("Period", list(PERIODS.keys()), index=0)
yf_period, yf_interval, x_fmt = PERIODS[period_choice]

INTERVALS = {"Off": None, "5 min": "5m", "10 min": "10m", "15 min": "15m"}
st.sidebar.markdown("**Auto-Refresh**")
refresh_choice = st.sidebar.selectbox(
    "Interval", list(INTERVALS.keys()),
    index=0 if period_choice != "Intraday" else 2,
    disabled=(period_choice != "Intraday"),
    help="Auto-refresh only applies to Intraday charts",
)
run_every = INTERVALS[refresh_choice] if period_choice == "Intraday" else None


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

    for group_name, tickers in GROUPS.items():
        st.subheader(group_name)
        for row_start in range(0, len(tickers), COLS):
            row  = tickers[row_start : row_start + COLS]
            cols = st.columns(COLS)
            for col, (label, ticker) in zip(cols, row):
                with col:
                    df, prev_close = fetch_ticker(ticker, yf_period, yf_interval)
                    _chart(label, ticker, df, prev_close, x_fmt)
        st.divider()


commodity_panel()
