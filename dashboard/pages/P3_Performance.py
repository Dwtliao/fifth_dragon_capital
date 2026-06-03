import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import query

st.set_page_config(page_title="Performance — Fifth Dragon Capital", layout="wide")
st.title("Performance")


# ── date range filter ─────────────────────────────────────────────────────────

RANGES = {"YTD": "ytd", "1 Year": "1y", "3 Years": "3y", "All": "all"}

sel_range = st.radio("Period", list(RANGES.keys()), horizontal=True, index=3)

today = pd.Timestamp.now().normalize()
if sel_range == "YTD":
    cutoff = pd.Timestamp(today.year, 1, 1)
elif sel_range == "1 Year":
    cutoff = today - pd.DateOffset(years=1)
elif sel_range == "3 Years":
    cutoff = today - pd.DateOffset(years=3)
else:
    cutoff = pd.Timestamp("2000-01-01")


# ── load data ─────────────────────────────────────────────────────────────────

ts_raw = pd.DataFrame(query("""
    SELECT date, total_market_value, daily_return_pct,
           rolling_30d_return_pct, rolling_90d_return_pct,
           rolling_volatility_30d, drawdown_from_peak_pct
    FROM mv_portfolio_timeseries
    ORDER BY date
"""))

bm_raw = pd.DataFrame(query("""
    SELECT date, portfolio_cumulative_pct, spy_cumulative_pct,
           portfolio_daily_return_pct, spy_daily_return_pct,
           rolling_30d_portfolio_pct, rolling_30d_spy_pct,
           alpha_pct
    FROM mv_benchmark_comparison
    ORDER BY date
"""))

rg_raw = pd.DataFrame(query("""
    SELECT
        EXTRACT(year FROM sell_date)::int   AS year,
        SUM(realized_pnl)::float            AS realized_pnl,
        SUM(CASE WHEN term = 'long'  THEN realized_pnl ELSE 0 END)::float AS long_term,
        SUM(CASE WHEN term = 'short' THEN realized_pnl ELSE 0 END)::float AS short_term,
        COUNT(*)                            AS lots
    FROM realized_gains
    GROUP BY 1
    ORDER BY 1
"""))

if ts_raw.empty:
    st.info("No portfolio timeseries data yet — run a sync first.")
    st.stop()

ts_raw["date"] = pd.to_datetime(ts_raw["date"])
bm_raw["date"] = pd.to_datetime(bm_raw["date"])

# Re-anchor cumulative returns to the selected period's start
ts = ts_raw[ts_raw["date"] >= cutoff].copy()
bm = bm_raw[bm_raw["date"] >= cutoff].copy()

if ts.empty:
    st.warning("No data in selected period.")
    st.stop()

# Re-anchor cumulative return to 0% at period start
period_base_port = ts["total_market_value"].iloc[0]
ts["cum_return_pct"] = (ts["total_market_value"] / period_base_port - 1) * 100

if not bm.empty:
    spy_base = bm["spy_cumulative_pct"].iloc[0]
    port_base = bm["portfolio_cumulative_pct"].iloc[0]
    bm["cum_port"] = bm["portfolio_cumulative_pct"] - port_base
    bm["cum_spy"]  = bm["spy_cumulative_pct"]       - spy_base


# ── KPIs ──────────────────────────────────────────────────────────────────────

total_return  = ts["cum_return_pct"].iloc[-1]
max_drawdown  = float(ts["drawdown_from_peak_pct"].min())
latest_vol    = ts["rolling_volatility_30d"].dropna().iloc[-1] if ts["rolling_volatility_30d"].notna().any() else None
spy_return    = bm["cum_spy"].iloc[-1]  if not bm.empty else None
alpha         = (total_return - spy_return) if spy_return is not None else None

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Portfolio Return",    f"{total_return:+.2f}%",
          help=f"Cumulative return over selected period")
k2.metric("SPY Return",          f"{spy_return:+.2f}%" if spy_return is not None else "—")
k3.metric("Alpha vs SPY",        f"{alpha:+.2f}%"      if alpha is not None else "—")
k4.metric("Max Drawdown",        f"{max_drawdown:.2f}%")
k5.metric("Volatility (30d ann)",f"{latest_vol:.1f}%"  if latest_vol is not None else "—",
          help="Annualised rolling 30-day volatility")

st.divider()


# ── equity curve ──────────────────────────────────────────────────────────────

st.subheader("Equity Curve")

if not bm.empty:
    port_line = bm[["date", "cum_port"]].rename(columns={"cum_port": "value"}).assign(series="Portfolio")
    spy_line  = bm[["date", "cum_spy" ]].rename(columns={"cum_spy":  "value"}).assign(series="SPY")
    curve_df  = pd.concat([port_line, spy_line])
else:
    curve_df = ts[["date", "cum_return_pct"]].rename(columns={"cum_return_pct": "value"}).assign(series="Portfolio")

equity_chart = (
    alt.Chart(curve_df)
    .mark_line()
    .encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("value:Q", title="Cumulative Return (%)"),
        color=alt.Color("series:N", scale=alt.Scale(
            domain=["Portfolio", "SPY"],
            range=["#1f77b4", "#ff7f0e"],
        )),
        tooltip=[
            alt.Tooltip("date:T", title="Date"),
            alt.Tooltip("series:N", title=""),
            alt.Tooltip("value:Q", title="Return %", format="+.2f"),
        ],
    )
    .properties(height=300)
)

zero_rule = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
    color="gray", strokeDash=[4, 4], opacity=0.5
).encode(y="y:Q")

st.altair_chart(equity_chart + zero_rule, use_container_width=True)

st.divider()


# ── drawdown chart ────────────────────────────────────────────────────────────

st.subheader("Drawdown from Peak")

dd_df = ts[["date", "drawdown_from_peak_pct"]].dropna()

drawdown_chart = (
    alt.Chart(dd_df)
    .mark_area(color="#d62728", opacity=0.4, line={"color": "#d62728"})
    .encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("drawdown_from_peak_pct:Q", title="Drawdown (%)",
                scale=alt.Scale(domainMax=0)),
        tooltip=[
            alt.Tooltip("date:T", title="Date"),
            alt.Tooltip("drawdown_from_peak_pct:Q", title="Drawdown %", format=".2f"),
        ],
    )
    .properties(height=200)
)

st.altair_chart(drawdown_chart, use_container_width=True)

st.divider()


# ── rolling returns vs SPY ────────────────────────────────────────────────────

st.subheader("Rolling 30-Day Return vs SPY")

if not bm.empty and bm["rolling_30d_portfolio_pct"].notna().any():
    roll_port = bm[["date", "rolling_30d_portfolio_pct"]].rename(
        columns={"rolling_30d_portfolio_pct": "value"}).assign(series="Portfolio")
    roll_spy  = bm[["date", "rolling_30d_spy_pct"]].rename(
        columns={"rolling_30d_spy_pct": "value"}).assign(series="SPY")
    roll_df   = pd.concat([roll_port, roll_spy]).dropna(subset=["value"])

    rolling_chart = (
        alt.Chart(roll_df)
        .mark_line()
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("value:Q", title="30-Day Return (%)"),
            color=alt.Color("series:N", scale=alt.Scale(
                domain=["Portfolio", "SPY"],
                range=["#1f77b4", "#ff7f0e"],
            )),
            tooltip=[
                alt.Tooltip("date:T", title="Date"),
                alt.Tooltip("series:N", title=""),
                alt.Tooltip("value:Q", title="30d Return %", format="+.2f"),
            ],
        )
        .properties(height=250)
    )
    st.altair_chart(rolling_chart + zero_rule, use_container_width=True)
else:
    st.info("Not enough data for rolling 30-day returns yet.")

st.divider()


# ── rolling volatility ────────────────────────────────────────────────────────

st.subheader("Rolling 30-Day Volatility (Annualised)")

vol_df = ts[["date", "rolling_volatility_30d"]].dropna()

if not vol_df.empty:
    vol_chart = (
        alt.Chart(vol_df)
        .mark_line(color="#9467bd")
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("rolling_volatility_30d:Q", title="Volatility (%)"),
            tooltip=[
                alt.Tooltip("date:T", title="Date"),
                alt.Tooltip("rolling_volatility_30d:Q", title="Volatility %", format=".1f"),
            ],
        )
        .properties(height=200)
    )
    st.altair_chart(vol_chart, use_container_width=True)
else:
    st.info("Not enough data for rolling volatility yet (need 30+ days).")

st.divider()


# ── realized P/L by year ──────────────────────────────────────────────────────

st.subheader("Realized P/L by Year")

if not rg_raw.empty:
    bar_df = rg_raw.copy()
    bar_df["color"] = bar_df["realized_pnl"].apply(lambda x: "gain" if x >= 0 else "loss")

    bar_chart = (
        alt.Chart(bar_df)
        .mark_bar()
        .encode(
            x=alt.X("year:O", title="Year"),
            y=alt.Y("realized_pnl:Q", title="Realized P/L ($)"),
            color=alt.Color("color:N", scale=alt.Scale(
                domain=["gain", "loss"], range=["#2ca02c", "#d62728"]
            ), legend=None),
            tooltip=[
                alt.Tooltip("year:O",          title="Year"),
                alt.Tooltip("realized_pnl:Q",  title="Total P/L ($)",  format=",.0f"),
                alt.Tooltip("long_term:Q",      title="Long-term ($)",  format=",.0f"),
                alt.Tooltip("short_term:Q",     title="Short-term ($)", format=",.0f"),
                alt.Tooltip("lots:Q",           title="Lots closed"),
            ],
        )
        .properties(height=250)
    )

    zero_bar = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
        color="gray", strokeDash=[4, 4], opacity=0.5
    ).encode(y="y:Q")

    st.altair_chart(bar_chart + zero_bar, use_container_width=True)

    # summary table
    summary = rg_raw.copy()
    summary["realized_pnl"] = summary["realized_pnl"].apply(lambda x: f"${x:,.0f}")
    summary["long_term"]    = summary["long_term"].apply(lambda x: f"${x:,.0f}")
    summary["short_term"]   = summary["short_term"].apply(lambda x: f"${x:,.0f}")
    summary.columns         = ["Year", "Total P/L", "Long-term", "Short-term", "Lots Closed"]
    st.dataframe(summary, use_container_width=True, hide_index=True)
else:
    st.info("No realized gains data yet — run build-realized-pnl first.")
