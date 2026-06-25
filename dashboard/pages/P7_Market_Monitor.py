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
    "Intraday": ("2d",  "5m",  "%H:%M"),
    "5 Days":   ("5d",  "15m", "%m/%d %H:%M"),
    "1 Month":  ("1mo", "1d",  "%b %d"),
    "3 Months": ("3mo", "1d",  "%b %d"),
    "6 Months": ("6mo", "1d",  "%b '%y"),
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


def _intraday_chart(label: str, ticker: str, today_df: pd.DataFrame, prev_close: float | None, fmt: str = "%H:%M", alerts: list[dict] | None = None, show_ema: bool = False) -> None:
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
            alt.Tooltip("Datetime:T", title="Time",  format=fmt),
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

    rsi = _rsi_panel(today_df, fmt)
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
                    _intraday_chart(label, ticker, df, prev_close, x_fmt, alerts_by_ticker.get(ticker), show_ema)
        st.divider()


market_panel()
