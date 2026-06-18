import subprocess
import sys
from pathlib import Path
from datetime import datetime

import altair as alt
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

COLS = 3  # charts per row


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
    today_df = df[df["Datetime"].dt.date == today][["Datetime", "Open", "High", "Low", "Close", "Volume"]].copy()
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

    today_df["color"] = (today_df["Close"] >= today_df["Open"]).map({True: "up", False: "down"})

    base = alt.Chart(today_df).encode(
        x=alt.X("Datetime:T", title=None,
                axis=alt.Axis(format="%H:%M", labelAngle=-45, tickCount=6)),
        color=alt.Color(
            "color:N",
            scale=alt.Scale(domain=["up", "down"], range=["#4CAF50", "#ef5350"]),
            legend=None,
        ),
        tooltip=[
            alt.Tooltip("Datetime:T", title="Time",  format="%H:%M"),
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
    )

    volume_chart = (
        alt.Chart(today_df)
        .mark_bar(size=5, stroke=None, opacity=0.7)
        .encode(
            x=alt.X("Datetime:T", title=None, axis=alt.Axis(format="%H:%M", labelAngle=-45, tickCount=6)),
            y=alt.Y("Volume:Q",   title=None, axis=alt.Axis(format="~s", tickCount=3)),
            color=alt.Color(
                "color:N",
                scale=alt.Scale(domain=["up", "down"], range=["#4CAF50", "#ef5350"]),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("Datetime:T", title="Time",   format="%H:%M"),
                alt.Tooltip("Volume:Q",   title="Volume", format=","),
            ],
        )
        .properties(height=60)
    )

    chart = alt.vconcat(candle_chart, volume_chart, spacing=4).resolve_scale(x="shared")
    st.altair_chart(chart, use_container_width=True)


# ── sidebar ────────────────────────────────────────────────────────────────────

st.sidebar.markdown("**Auto-Refresh**")
INTERVALS = {"Off": None, "5 min": "5m", "10 min": "10m", "15 min": "15m"}
choice   = st.sidebar.selectbox("Interval", list(INTERVALS.keys()), index=2)
run_every = INTERVALS[choice]

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
