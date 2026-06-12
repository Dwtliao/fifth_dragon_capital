import sys
from pathlib import Path
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

st.set_page_config(page_title="Market Monitor — Fifth Dragon Capital", layout="wide")
st.title("Market Monitor")

GROUPS = {
    "US Indices": [
        ("S&P 500",        "^GSPC"),
        ("Dow Jones",      "^DJI"),
        ("NASDAQ",         "^IXIC"),
        ("Russell 2000",   "^RUT"),
        ("NYSE Composite", "^NYA"),
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
    "Volatility & Rates": [
        ("VIX",       "^VIX"),
        ("VVIX",      "^VVIX"),
        ("10Y Yield", "^TNX"),
    ],
}

COLS = 4  # charts per row


@st.cache_data(ttl=300)
def fetch_ticker(ticker: str) -> tuple[pd.DataFrame, float | None]:
    """Returns (today_5m_bars, prev_close). One yfinance call per ticker."""
    df = yf.Ticker(ticker).history(period="2d", interval="5m")
    if df.empty:
        return pd.DataFrame(), None
    df = df.reset_index()
    df["Datetime"] = pd.to_datetime(df["Datetime"]).dt.tz_localize(None)
    today = df["Datetime"].dt.date.max()
    prev_closes = df[df["Datetime"].dt.date < today]["Close"]
    prev_close = float(prev_closes.iloc[-1]) if not prev_closes.empty else None
    today_df = df[df["Datetime"].dt.date == today][["Datetime", "Close"]].copy()
    return today_df, prev_close


def _intraday_chart(label: str, ticker: str, today_df: pd.DataFrame, prev_close: float | None) -> None:
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

    chart = (
        alt.Chart(today_df)
        .mark_line(color=line_color, strokeWidth=1.5)
        .encode(
            x=alt.X("Datetime:T", title=None,
                    axis=alt.Axis(format="%H:%M", labelAngle=-45, tickCount=6)),
            y=alt.Y("Close:Q", title=None, scale=alt.Scale(zero=False),
                    axis=alt.Axis(format=",.2f")),
            tooltip=[
                alt.Tooltip("Datetime:T", title="Time",  format="%H:%M"),
                alt.Tooltip("Close:Q",    title="Price", format=",.2f"),
            ],
        )
        .properties(
            title=alt.TitleParams(
                text=f"{label}  ({ticker})",
                subtitle=subtitle,
                fontSize=13,
                subtitleFontSize=11,
                subtitleColor=line_color,
            ),
            height=150,
        )
    )
    st.altair_chart(chart, use_container_width=True)


# ── sidebar ────────────────────────────────────────────────────────────────────

st.sidebar.markdown("**Auto-Refresh**")
INTERVALS = {"Off": None, "5 min": "5m", "10 min": "10m", "15 min": "15m"}
choice   = st.sidebar.selectbox("Interval", list(INTERVALS.keys()), index=2)
run_every = INTERVALS[choice]

# ── market data fragment (auto-refreshes independently) ───────────────────────

@st.fragment(run_every=run_every)
def market_panel() -> None:
    st.caption(
        f"Last updated: {datetime.now().strftime('%H:%M:%S')}  •  "
        "Data delayed ~15 min  •  Intraday = today's 5-min bars"
    )
    if st.button("↺ Refresh now", key="manual_refresh"):
        st.cache_data.clear()
        st.rerun(scope="fragment")

    for group_name, tickers in GROUPS.items():
        st.subheader(group_name)
        for row_start in range(0, len(tickers), COLS):
            row  = tickers[row_start : row_start + COLS]
            cols = st.columns(COLS)
            for col, (label, ticker) in zip(cols, row):
                with col:
                    today_df, prev_close = fetch_ticker(ticker)
                    _intraday_chart(label, ticker, today_df, prev_close)
        st.divider()


market_panel()
