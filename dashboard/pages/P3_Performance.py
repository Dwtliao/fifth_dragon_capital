import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import query

st.set_page_config(page_title="Performance — Fifth Dragon Capital", layout="wide")
st.title("Performance")


# ── helpers ───────────────────────────────────────────────────────────────────

def _zero_rule():
    return (
        alt.Chart(pd.DataFrame({"y": [0]}))
        .mark_rule(color="gray", strokeDash=[4, 4], opacity=0.5)
        .encode(y="y:Q")
    )


def _reanchor(df, cum_col):
    """Shift a cumulative-return column so the period starts at 0%."""
    base = df[cum_col].iloc[0]
    df = df.copy()
    df[cum_col] = df[cum_col] - base
    return df


# ── accounts ──────────────────────────────────────────────────────────────────

accounts_raw = query("""
    SELECT account_id_key, account_id,
           NULLIF(account_name, '') AS account_name,
           account_type
    FROM accounts ORDER BY account_name NULLS LAST, account_type
""")

def _acct_label(a):
    name = a["account_name"] or a["account_type"].replace("_", " ").title()
    return f"{name} ({a['account_id']})"

acct_options = {"All Accounts": None} | {_acct_label(a): a["account_id_key"] for a in accounts_raw}


# ── global filters ────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Filters")
    sel_account = st.selectbox("Account", list(acct_options.keys()))
    sel_period  = st.selectbox("Period", ["YTD", "1 Year", "3 Years", "All"], index=3)
    show_spy    = st.toggle("Show SPY", value=True)
    st.divider()
    rg_years_raw = query("""
        SELECT DISTINCT EXTRACT(year FROM sell_date)::int AS yr
        FROM realized_gains ORDER BY 1 DESC
    """)
    year_opts = ["All Years"] + [str(r["yr"]) for r in rg_years_raw]
    sel_year  = st.selectbox("Realized P/L Year", year_opts)

account_filter = acct_options[sel_account]
_acct_where  = "AND account_id_key = %(acct)s" if account_filter else ""
_acct_params = {"acct": account_filter}       if account_filter else {}

today = pd.Timestamp.now().normalize()
if sel_period == "YTD":
    cutoff = pd.Timestamp(today.year, 1, 1)
elif sel_period == "1 Year":
    cutoff = today - pd.DateOffset(years=1)
elif sel_period == "3 Years":
    cutoff = today - pd.DateOffset(years=3)
else:
    cutoff = pd.Timestamp("2000-01-01")

cutoff_s = cutoff.strftime("%Y-%m-%d")


# ── load timeseries + benchmark ───────────────────────────────────────────────

if account_filter:
    ts_raw = pd.DataFrame(query(f"""
        SELECT date, total_market_value, daily_return_pct,
               rolling_30d_return_pct, rolling_volatility_30d, drawdown_from_peak_pct
        FROM mv_portfolio_timeseries_by_account
        WHERE account_id_key = %(acct)s AND date >= %(cutoff)s
        ORDER BY date
    """, {"acct": account_filter, "cutoff": cutoff_s}))

    bm_raw = pd.DataFrame(query(f"""
        SELECT date, portfolio_cumulative_pct, spy_cumulative_pct,
               rolling_30d_portfolio_pct, rolling_30d_spy_pct, alpha_pct
        FROM mv_benchmark_comparison_by_account
        WHERE account_id_key = %(acct)s AND date >= %(cutoff)s
        ORDER BY date
    """, {"acct": account_filter, "cutoff": cutoff_s}))
else:
    ts_raw = pd.DataFrame(query(f"""
        SELECT date, total_market_value, daily_return_pct,
               rolling_30d_return_pct, rolling_volatility_30d, drawdown_from_peak_pct
        FROM mv_portfolio_timeseries
        WHERE date >= %(cutoff)s
        ORDER BY date
    """, {"cutoff": cutoff_s}))

    bm_raw = pd.DataFrame(query(f"""
        SELECT date, portfolio_cumulative_pct, spy_cumulative_pct,
               rolling_30d_portfolio_pct, rolling_30d_spy_pct, alpha_pct
        FROM mv_benchmark_comparison
        WHERE date >= %(cutoff)s
        ORDER BY date
    """, {"cutoff": cutoff_s}))

if ts_raw.empty:
    st.info("No portfolio data yet — run a sync first.")
    st.stop()

ts_raw["date"] = pd.to_datetime(ts_raw["date"])
bm_raw["date"] = pd.to_datetime(bm_raw["date"])

# Re-anchor cumulative returns to period start
bm = _reanchor(bm_raw, "portfolio_cumulative_pct").rename(
    columns={"portfolio_cumulative_pct": "cum_port", "spy_cumulative_pct": "cum_spy_raw"}
) if not bm_raw.empty else bm_raw.copy()

if not bm.empty:
    spy_base     = bm_raw["spy_cumulative_pct"].iloc[0]
    bm["cum_spy"] = bm_raw["spy_cumulative_pct"] - spy_base


# ── KPIs ──────────────────────────────────────────────────────────────────────

total_return = float(bm["cum_port"].iloc[-1])           if not bm.empty else None
spy_return   = float(bm["cum_spy"].iloc[-1])            if not bm.empty and show_spy else None
alpha        = (total_return - spy_return)               if total_return is not None and spy_return is not None else None
max_dd       = float(ts_raw["drawdown_from_peak_pct"].min())
latest_vol   = ts_raw["rolling_volatility_30d"].dropna()
latest_vol   = float(latest_vol.iloc[-1]) if not latest_vol.empty else None

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Portfolio Return",     f"{total_return:+.2f}%" if total_return is not None else "—")
k2.metric("SPY Return",           f"{spy_return:+.2f}%"  if spy_return   is not None else "—")
k3.metric("Alpha vs SPY",         f"{alpha:+.2f}%"       if alpha        is not None else "—")
k4.metric("Max Drawdown",         f"{max_dd:.2f}%")
k5.metric("Volatility (30d ann)", f"{latest_vol:.1f}%"   if latest_vol   is not None else "—",
          help="Annualised rolling 30-day volatility")

st.divider()


# ── equity curve ──────────────────────────────────────────────────────────────

st.subheader("Equity Curve")

if not bm.empty:
    lines = [bm[["date", "cum_port"]].rename(columns={"cum_port": "value"}).assign(series="Portfolio")]
    if show_spy:
        lines.append(bm[["date", "cum_spy"]].rename(columns={"cum_spy": "value"}).assign(series="SPY"))
    curve_df = pd.concat(lines)
else:
    mv_base  = ts_raw["total_market_value"].iloc[0]
    ts_raw["cum_port"] = (ts_raw["total_market_value"] / mv_base - 1) * 100
    curve_df = ts_raw[["date", "cum_port"]].rename(columns={"cum_port": "value"}).assign(series="Portfolio")

equity_chart = (
    alt.Chart(curve_df)
    .mark_line()
    .encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("value:Q", title="Cumulative Return (%)"),
        color=alt.Color("series:N", scale=alt.Scale(
            domain=["Portfolio", "SPY"], range=["#1f77b4", "#ff7f0e"]
        )),
        tooltip=[
            alt.Tooltip("date:T",    title="Date"),
            alt.Tooltip("series:N",  title=""),
            alt.Tooltip("value:Q",   title="Return %", format="+.2f"),
        ],
    )
    .properties(height=300)
)
st.altair_chart(equity_chart + _zero_rule(), use_container_width=True)

st.divider()


# ── drawdown ──────────────────────────────────────────────────────────────────

st.subheader("Drawdown from Peak")

dd_df = ts_raw[["date", "drawdown_from_peak_pct"]].dropna()
if not dd_df.empty:
    st.altair_chart(
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
        .properties(height=200),
        use_container_width=True,
    )

st.divider()


# ── rolling 30-day return ─────────────────────────────────────────────────────

st.subheader("Rolling 30-Day Return vs SPY")

has_rolling = not bm.empty and bm["rolling_30d_portfolio_pct"].notna().any()
if has_rolling:
    roll_lines = [
        bm[["date", "rolling_30d_portfolio_pct"]]
        .rename(columns={"rolling_30d_portfolio_pct": "value"})
        .assign(series="Portfolio")
    ]
    if show_spy:
        roll_lines.append(
            bm[["date", "rolling_30d_spy_pct"]]
            .rename(columns={"rolling_30d_spy_pct": "value"})
            .assign(series="SPY")
        )
    roll_df = pd.concat(roll_lines).dropna(subset=["value"])
    st.altair_chart(
        alt.Chart(roll_df)
        .mark_line()
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("value:Q", title="30-Day Return (%)"),
            color=alt.Color("series:N", scale=alt.Scale(
                domain=["Portfolio", "SPY"], range=["#1f77b4", "#ff7f0e"]
            )),
            tooltip=[
                alt.Tooltip("date:T",   title="Date"),
                alt.Tooltip("series:N", title=""),
                alt.Tooltip("value:Q",  title="30d Return %", format="+.2f"),
            ],
        )
        .properties(height=250)
        + _zero_rule(),
        use_container_width=True,
    )
else:
    first_date = ts_raw["date"].min().strftime("%Y-%m-%d")
    st.info(f"Rolling 30-day data will appear once 30 days of daily syncs have accumulated. "
            f"Earliest snapshot: {first_date}.")

st.divider()


# ── attribution ───────────────────────────────────────────────────────────────

st.subheader("Attribution")

attr_raw = pd.DataFrame(query(f"""
    SELECT date, account_id_key, sector, asset_class,
           market_value::float, cost_basis::float,
           unrealized_pnl::float, pct_of_account::float
    FROM mv_attribution_timeseries
    WHERE date >= %(cutoff)s {_acct_where}
    ORDER BY date
""", {"cutoff": cutoff_s, **_acct_params}))

tab_acct, tab_sector, tab_ac = st.tabs(["By Account", "By Sector", "By Asset Class"])


# ── attribution: by account ───────────────────────────────────────────────────

with tab_acct:
    if account_filter:
        st.info("Select **All Accounts** in the global filter to compare accounts side by side.")
    else:
        acct_ts_raw = pd.DataFrame(query(f"""
            SELECT t.account_id_key, t.date,
                   t.total_market_value::float,
                   COALESCE(a.account_name, a.account_type) AS account_name,
                   a.account_id
            FROM mv_portfolio_timeseries_by_account t
            JOIN accounts a USING (account_id_key)
            WHERE t.date >= %(cutoff)s
            ORDER BY t.account_id_key, t.date
        """, {"cutoff": cutoff_s}))

        if acct_ts_raw.empty:
            st.info("No per-account data yet.")
        else:
            acct_ts_raw["date"]  = pd.to_datetime(acct_ts_raw["date"])
            acct_ts_raw["label"] = acct_ts_raw.apply(
                lambda r: f"{r['account_name']} ({r['account_id']})", axis=1
            )
            # Re-anchor each account to 0% at period start
            rows = []
            for label, grp in acct_ts_raw.groupby("label"):
                base = grp["total_market_value"].iloc[0]
                grp = grp.copy()
                grp["cum_return"] = (grp["total_market_value"] / base - 1) * 100
                rows.append(grp)
            acct_curve = pd.concat(rows)

            st.altair_chart(
                alt.Chart(acct_curve)
                .mark_line()
                .encode(
                    x=alt.X("date:T", title=None),
                    y=alt.Y("cum_return:Q", title="Cumulative Return (%)"),
                    color=alt.Color("label:N", title="Account"),
                    tooltip=[
                        alt.Tooltip("date:T",       title="Date"),
                        alt.Tooltip("label:N",       title="Account"),
                        alt.Tooltip("cum_return:Q",  title="Return %", format="+.2f"),
                    ],
                )
                .properties(height=300)
                + _zero_rule(),
                use_container_width=True,
            )


# ── attribution: by sector ────────────────────────────────────────────────────

def _attribution_charts(attr_df, dim_col, dim_title):
    if attr_df.empty:
        st.info(f"No attribution data for the selected period.")
        return

    attr_df = attr_df.copy()
    attr_df["date"] = pd.to_datetime(attr_df["date"])

    # Stacked area: market value over time by dimension
    area_df = (
        attr_df.groupby(["date", dim_col], as_index=False)
        .agg(market_value=("market_value", "sum"))
    )
    st.altair_chart(
        alt.Chart(area_df)
        .mark_area()
        .encode(
            x=alt.X("date:T", title=None),
            y=alt.Y("market_value:Q", stack=True, title="Market Value ($)",
                    axis=alt.Axis(format="$,.0f")),
            color=alt.Color(f"{dim_col}:N", title=dim_title),
            tooltip=[
                alt.Tooltip("date:T",          title="Date"),
                alt.Tooltip(f"{dim_col}:N",    title=dim_title),
                alt.Tooltip("market_value:Q",  title="Market Value ($)", format="$,.0f"),
            ],
        )
        .properties(height=280, title=f"{dim_title} Allocation Over Time"),
        use_container_width=True,
    )

    # Bar chart: current unrealized P/L by dimension (most recent date)
    latest_date = attr_df["date"].max()
    bar_df = (
        attr_df[attr_df["date"] == latest_date]
        .groupby(dim_col, as_index=False)
        .agg(
            market_value  =("market_value",   "sum"),
            unrealized_pnl=("unrealized_pnl", "sum"),
        )
        .sort_values("market_value", ascending=False)
    )
    bar_df["color"] = bar_df["unrealized_pnl"].apply(lambda x: "gain" if x >= 0 else "loss")

    st.altair_chart(
        alt.Chart(bar_df)
        .mark_bar()
        .encode(
            x=alt.X(f"{dim_col}:N", sort="-y", title=dim_title),
            y=alt.Y("unrealized_pnl:Q", title="Unrealized P/L ($)",
                    axis=alt.Axis(format="$,.0f")),
            color=alt.Color("color:N", scale=alt.Scale(
                domain=["gain", "loss"], range=["#2ca02c", "#d62728"]
            ), legend=None),
            tooltip=[
                alt.Tooltip(f"{dim_col}:N",     title=dim_title),
                alt.Tooltip("market_value:Q",   title="Market Value ($)",   format="$,.0f"),
                alt.Tooltip("unrealized_pnl:Q", title="Unrealized P/L ($)", format="$,.0f"),
            ],
        )
        .properties(height=220, title=f"Current Unrealized P/L by {dim_title}")
        + alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
            color="gray", strokeDash=[4, 4], opacity=0.5
        ).encode(y="y:Q"),
        use_container_width=True,
    )


with tab_sector:
    _attribution_charts(attr_raw, "sector", "Sector")

with tab_ac:
    _attribution_charts(attr_raw, "asset_class", "Asset Class")

st.divider()


# ── realized P/L ──────────────────────────────────────────────────────────────

st.subheader("Realized P/L")

_year_where  = "AND EXTRACT(year FROM sell_date) = %(yr)s" if sel_year != "All Years" else ""
_year_params = {"yr": int(sel_year)} if sel_year != "All Years" else {}

rg_by_year = pd.DataFrame(query(f"""
    SELECT
        EXTRACT(year FROM sell_date)::int AS year,
        SUM(realized_pnl)::float          AS realized_pnl,
        SUM(CASE WHEN term='long'  THEN realized_pnl ELSE 0 END)::float AS long_term,
        SUM(CASE WHEN term='short' THEN realized_pnl ELSE 0 END)::float AS short_term,
        COUNT(*)                          AS lots
    FROM realized_gains
    WHERE 1=1 {_acct_where} {_year_where}
    GROUP BY 1 ORDER BY 1
""", {**_acct_params, **_year_params}))

if not rg_by_year.empty:
    rg_by_year["color"] = rg_by_year["realized_pnl"].apply(lambda x: "gain" if x >= 0 else "loss")
    bar = (
        alt.Chart(rg_by_year)
        .mark_bar()
        .encode(
            x=alt.X("year:O", title="Year"),
            y=alt.Y("realized_pnl:Q", title="Realized P/L ($)", axis=alt.Axis(format="$,.0f")),
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
    st.altair_chart(
        bar + alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
            color="gray", strokeDash=[4, 4], opacity=0.5
        ).encode(y="y:Q"),
        use_container_width=True,
    )

    # Lot detail table
    lots_df = pd.DataFrame(query(f"""
        SELECT
            sell_date, symbol, quantity::float,
            buy_price::float, sell_price::float,
            cost_basis::float, proceeds::float,
            realized_pnl::float, term
        FROM realized_gains
        WHERE 1=1 {_acct_where} {_year_where}
        ORDER BY sell_date DESC, realized_pnl DESC
    """, {**_acct_params, **_year_params}))

    if not lots_df.empty:
        with st.expander(f"Lot detail — {sel_year}", expanded=False):
            lots_df["sell_date"]    = pd.to_datetime(lots_df["sell_date"]).dt.strftime("%Y-%m-%d")
            lots_df["realized_pnl"] = lots_df["realized_pnl"].apply(lambda x: f"${x:,.0f}")
            lots_df["proceeds"]     = lots_df["proceeds"].apply(lambda x: f"${x:,.0f}")
            lots_df["cost_basis"]   = lots_df["cost_basis"].apply(lambda x: f"${x:,.0f}")
            lots_df["buy_price"]    = lots_df["buy_price"].apply(lambda x: f"${x:,.2f}")
            lots_df["sell_price"]   = lots_df["sell_price"].apply(lambda x: f"${x:,.2f}")
            lots_df["quantity"]     = lots_df["quantity"].apply(
                lambda x: f"{x:,.0f}" if x == int(x) else f"{x:,.4f}"
            )
            lots_df.columns = [
                "Sell Date", "Symbol", "Qty", "Buy Price", "Sell Price",
                "Cost Basis", "Proceeds", "Realized P/L", "Term",
            ]
            st.dataframe(lots_df, use_container_width=True, hide_index=True)
else:
    st.info("No realized gains data yet — run build-realized-pnl from the Pipeline Status page.")
