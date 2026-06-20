import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import query

st.set_page_config(page_title="Risk & Exposure — Fifth Dragon Capital", layout="wide")
st.title("Risk & Exposure")


# ── account filter ─────────────────────────────────────────────────────────────

accounts = query("""
    SELECT account_id_key, account_id,
           NULLIF(account_name, '') AS account_name,
           account_type
    FROM accounts
    ORDER BY account_name NULLS LAST, account_type
""")

def account_label(a):
    name = a["account_name"] or a["account_type"].replace("_", " ").title()
    return f"{name} ({a['account_id']})"

account_options = {"All Accounts": None} | {
    account_label(a): a["account_id_key"] for a in accounts
}

selected_account = st.sidebar.selectbox("Account", list(account_options.keys()))
account_filter   = account_options[selected_account]
_where  = "AND account_id_key = %(acct)s" if account_filter else ""
_params = {"acct": account_filter} if account_filter else {}

# ── concentration thresholds (Section 2) ──────────────────────────────────────

st.sidebar.divider()
st.sidebar.markdown("**Concentration Thresholds**")
st.sidebar.caption("Applied to % of risk assets (excl. Cash & Fixed Income)")
thresh_overweight   = st.sidebar.number_input("🟡 Overweight ≥ (%)",   min_value=1, max_value=99,  value=10, step=1)
thresh_concentrated = st.sidebar.number_input("🔴 Concentrated ≥ (%)", min_value=1, max_value=100, value=20, step=1)
recompute = st.sidebar.button("Recompute", type="primary", use_container_width=True)

if "applied_thresholds" not in st.session_state:
    st.session_state.applied_thresholds = (10, 20)
if recompute:
    st.session_state.applied_thresholds = (int(thresh_overweight), int(thresh_concentrated))

t_over, t_conc = st.session_state.applied_thresholds


# ── fetch allocations ──────────────────────────────────────────────────────────

alloc = pd.DataFrame(query(f"""
    SELECT symbol, sector, asset_class,
           SUM(market_value)::float     AS market_value,
           SUM(cost_basis)::float       AS cost_basis,
           SUM(pct_of_portfolio)::float AS pct_of_portfolio,
           SUM(unrealized_pnl)::float   AS unrealized_pnl
    FROM mv_allocations
    WHERE 1=1 {_where}
    GROUP BY symbol, sector, asset_class
    ORDER BY market_value DESC
""", _params))

if alloc.empty:
    st.info("No position data available.")
    st.stop()

_EXCLUDE_CLASSES = {"Cash", "Fixed Income"}

# Portfolio exposure denominator — all assets, sums to 100%
total_mv = alloc["market_value"].sum()
alloc["pct_of_portfolio"] = alloc["market_value"] / total_mv * 100 if total_mv else 0

# Risk concentration denominator — risk assets only (excl. Cash + Fixed Income)
risk_mv = alloc.loc[~alloc["asset_class"].isin(_EXCLUDE_CLASSES), "market_value"].sum()
alloc["pct_of_risk_assets"] = alloc["market_value"] / risk_mv * 100 if risk_mv else 0
alloc.loc[alloc["asset_class"].isin(_EXCLUDE_CLASSES), "pct_of_risk_assets"] = 0


# ── portfolio snapshot strip ───────────────────────────────────────────────────

risk_alloc  = alloc[~alloc["asset_class"].isin(_EXCLUDE_CLASSES)].copy()
risk_sorted = risk_alloc.sort_values("pct_of_risk_assets", ascending=False)

largest  = risk_sorted.iloc[0] if not risk_sorted.empty else None
top3_pct = risk_sorted.head(3)["pct_of_risk_assets"].sum()
cash_pct  = alloc.loc[alloc["asset_class"] == "Cash",      "pct_of_portfolio"].sum()
comm_pct  = alloc.loc[alloc["asset_class"] == "Commodity", "pct_of_portfolio"].sum()

s1, s2, s3, s4 = st.columns(4)
if largest is not None:
    s1.metric(
        "Largest Position",
        f"{largest['symbol']}  {largest['pct_of_risk_assets']:.1f}%",
        help="Largest single holding as % of risk assets (excl. Cash & Fixed Income)",
    )
s2.metric(
    "Top 3 Concentration",
    f"{top3_pct:.1f}%",
    help=f"% of risk assets — {', '.join(risk_sorted.head(3)['symbol'].tolist())}",
)
s3.metric(
    "Cash Reserve",
    f"{cash_pct:.1f}%",
    help="Cash & equivalents as % of total portfolio",
)
s4.metric(
    "Commodity Tilt",
    f"{comm_pct:.1f}%",
    help="Commodity positions as % of total portfolio",
)

st.divider()


# ── Section 1: Concentration Risk ─────────────────────────────────────────────

st.header("Concentration Risk")

st.divider()

# ── concentration bar charts ──────────────────────────────────────────────────

col_left, col_right = st.columns(2)

with col_left:
    st.subheader("By Sector")
    st.caption("% of risk assets — Cash & Fixed Income excluded")

    sector_df = (
        risk_alloc
        .groupby("sector", as_index=False)["pct_of_risk_assets"]
        .sum()
        .sort_values("pct_of_risk_assets", ascending=False)
    )

    if sector_df.empty:
        st.info("No risk asset positions.")
    else:
        chart_s = (
            alt.Chart(sector_df)
            .mark_bar()
            .encode(
                x=alt.X("pct_of_risk_assets:Q", title="% of Risk Assets", axis=alt.Axis(format=".1f")),
                y=alt.Y("sector:N", sort="-x", title=None),
                tooltip=[
                    alt.Tooltip("sector:N",             title="Sector"),
                    alt.Tooltip("pct_of_risk_assets:Q", title="% Risk Assets", format=".1f"),
                ],
            )
            .properties(height=max(120, len(sector_df) * 35))
        )
        st.altair_chart(chart_s, use_container_width=True)

with col_right:
    st.subheader("By Asset Class")
    st.caption("% of total portfolio — all assets including Cash & Fixed Income")

    ac_df = (
        alloc.groupby("asset_class", as_index=False)["pct_of_portfolio"]
        .sum()
        .sort_values("pct_of_portfolio", ascending=False)
    )

    chart_a = (
        alt.Chart(ac_df)
        .mark_bar()
        .encode(
            x=alt.X("pct_of_portfolio:Q", title="% of Portfolio", axis=alt.Axis(format=".1f")),
            y=alt.Y("asset_class:N", sort="-x", title=None),
            color=alt.Color(
                "asset_class:N",
                scale=alt.Scale(
                    domain=["Cash", "Fixed Income", "Equity", "Commodity"],
                    range=["#4CAF50", "#2196F3", "#FF9800", "#9C27B0"],
                ),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("asset_class:N",    title="Asset Class"),
                alt.Tooltip("pct_of_portfolio:Q", title="% Portfolio", format=".1f"),
            ],
        )
        .properties(height=max(120, len(ac_df) * 35))
    )
    st.altair_chart(chart_a, use_container_width=True)

st.divider()

# ── thematic exposure callout ─────────────────────────────────────────────────

st.subheader("Thematic Exposure")
st.caption("% of risk assets (excl. Cash & Fixed Income). Values may overlap — a position can carry multiple tags.")

CALLOUT_TAGS = ["Precious Metals", "Uranium", "Broad Energy", "Copper", "Agriculture", "Volatility"]

themes_raw = query(f"""
    SELECT t.tag,
           SUM(a.market_value)::float AS market_value
    FROM mv_allocations a
    JOIN symbol_exposure_tags t ON t.symbol = a.symbol
    WHERE t.tag = ANY(%(tags)s)
      AND a.asset_class NOT IN ('Cash', 'Fixed Income')
      {'AND a.account_id_key = %(acct)s' if account_filter else ''}
    GROUP BY t.tag
    ORDER BY market_value DESC
""", {"tags": CALLOUT_TAGS, "acct": account_filter} if account_filter else {"tags": CALLOUT_TAGS})

if not themes_raw:
    st.info("No thematic exposure tags defined. Add tags in Symbol Admin → Exposure Tags.")
else:
    themes_df = pd.DataFrame(themes_raw)
    themes_df["pct_of_risk_assets"] = themes_df["market_value"] / risk_mv * 100 if risk_mv else 0
    cols = st.columns(len(themes_df))
    for col, (_, row) in zip(cols, themes_df.iterrows()):
        col.metric(
            row["tag"],
            f"{row['pct_of_risk_assets']:.1f}%",
            f"${row['market_value']:,.0f}",
            delta_color="off",
        )

st.divider()


# ── Section 2: Position Sizing ────────────────────────────────────────────────

st.header("Position Sizing")
st.caption(
    f"Risk assets only — Cash and Fixed Income excluded. "
    f"Thresholds: 🟡 ≥ {t_over}%  🔴 ≥ {t_conc}% of risk assets."
)

over_count = (risk_alloc["pct_of_risk_assets"] >= t_over).sum()
max_wt     = risk_sorted["pct_of_risk_assets"].max() if not risk_sorted.empty else 0
max_sym    = risk_sorted.iloc[0]["symbol"] if not risk_sorted.empty else "—"

k1, k2 = st.columns(2)
k1.metric(
    f"Positions ≥ {t_over}% of Risk Assets",
    int(over_count),
    help=f"Count of risk positions individually representing ≥ {t_over}% of total risk asset value",
)
k2.metric(
    "Largest Risk Position",
    f"{max_sym}  {max_wt:.1f}%",
    help="Single risk position with highest % of risk assets",
)

st.divider()

def _size_flag(pct):
    if pct >= t_conc:
        return "🔴 Concentrated"
    if pct >= t_over:
        return "🟡 Overweight"
    return ""

sizing = risk_sorted[["symbol", "sector", "asset_class", "market_value", "pct_of_risk_assets"]].copy()
sizing["Flag"] = sizing["pct_of_risk_assets"].apply(_size_flag)
sizing.columns = ["Symbol", "Sector", "Asset Class", "Market Value", "% Risk Assets", "Flag"]

st.dataframe(
    sizing,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Market Value":   st.column_config.NumberColumn(format="$%.0f"),
        "% Risk Assets":  st.column_config.NumberColumn(format="%.1f%%"),
    },
)

st.divider()


# ── Section 3: Unrealized Loss Watch ─────────────────────────────────────────

st.header("Unrealized Loss Watch")
st.caption("Positions currently underwater. Cash excluded; Fixed Income included.")

losers = (
    alloc[
        (alloc["asset_class"] != "Cash") &
        (alloc["unrealized_pnl"] < 0)
    ]
    .copy()
    .sort_values("unrealized_pnl")
)

if losers.empty:
    st.success("No underwater positions — all holdings are at a gain.")
else:
    total_loss = losers["unrealized_pnl"].sum()
    n_losers   = len(losers)

    k1, k2 = st.columns(2)
    k1.metric(
        "Total Unrealized Loss",
        f"${total_loss:,.0f}",
        help="Sum of unrealized P/L across all underwater positions",
    )
    k2.metric(
        "Positions Underwater",
        int(n_losers),
        help="Number of non-cash holdings with negative unrealized P/L",
    )

    st.divider()

    chart_losers = (
        alt.Chart(losers)
        .mark_bar(color="#ef5350")
        .encode(
            x=alt.X("unrealized_pnl:Q", title="Unrealized P/L ($)", axis=alt.Axis(format="$,.0f")),
            y=alt.Y("symbol:N", sort="x", title=None),
            tooltip=[
                alt.Tooltip("symbol:N",           title="Symbol"),
                alt.Tooltip("sector:N",            title="Sector"),
                alt.Tooltip("unrealized_pnl:Q",    title="Unrealized P/L", format="$,.0f"),
                alt.Tooltip("pct_of_portfolio:Q",  title="% Portfolio",    format=".1f"),
            ],
        )
        .properties(height=max(120, n_losers * 32))
    )
    st.altair_chart(chart_losers, use_container_width=True)

    loss_display = losers[["symbol", "sector", "asset_class", "cost_basis", "market_value", "unrealized_pnl", "pct_of_portfolio"]].copy()
    loss_display["pnl_pct"] = loss_display["unrealized_pnl"] / loss_display["cost_basis"].replace(0, float("nan")) * 100
    loss_display.columns = ["Symbol", "Sector", "Asset Class", "Cost Basis", "Market Value", "Unrealized P/L", "% Portfolio", "P/L %"]
    st.dataframe(
        loss_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Cost Basis":     st.column_config.NumberColumn(format="$%.0f"),
            "Market Value":   st.column_config.NumberColumn(format="$%.0f"),
            "Unrealized P/L": st.column_config.NumberColumn(format="$%.0f"),
            "% Portfolio":    st.column_config.NumberColumn(format="%.1f%%"),
            "P/L %":          st.column_config.NumberColumn(format="%.1f%%"),
        },
    )

st.divider()


# ── Section 4: Realized P/L Summary ──────────────────────────────────────────

st.header("Realized P/L Summary")

rg_raw = pd.DataFrame(query(f"""
    SELECT symbol,
           EXTRACT(year FROM sell_date)::int AS year,
           realized_pnl::float               AS realized_pnl,
           proceeds::float                   AS proceeds,
           cost_basis::float                 AS cost_basis,
           holding_days,
           term
    FROM realized_gains
    WHERE 1=1 {_where}
""", _params))

if rg_raw.empty:
    st.info("No realized gains/losses on record.")
else:
    total_pnl = rg_raw["realized_pnl"].sum()
    n_trades  = len(rg_raw)
    n_winners = (rg_raw["realized_pnl"] > 0).sum()
    win_rate  = n_winners / n_trades * 100 if n_trades else 0

    k1, k2, k3 = st.columns(3)
    k1.metric("Total Realized P/L", f"${total_pnl:,.0f}")
    k2.metric("Closed Trades",       int(n_trades))
    k3.metric("Win Rate",            f"{win_rate:.1f}%",
              help="% of closed trades that were profitable")

    st.divider()

    by_year = (
        rg_raw.groupby("year", as_index=False)["realized_pnl"]
        .sum()
        .sort_values("year")
    )
    by_year["color"] = by_year["realized_pnl"].apply(lambda x: "gain" if x >= 0 else "loss")

    chart_year = (
        alt.Chart(by_year)
        .mark_bar()
        .encode(
            x=alt.X("year:O", title="Year"),
            y=alt.Y("realized_pnl:Q", title="Realized P/L ($)", axis=alt.Axis(format="$,.0f")),
            color=alt.Color(
                "color:N",
                scale=alt.Scale(domain=["gain", "loss"], range=["#4CAF50", "#ef5350"]),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("year:O",         title="Year"),
                alt.Tooltip("realized_pnl:Q", title="Realized P/L", format="$,.0f"),
            ],
        )
        .properties(height=240)
    )
    st.altair_chart(chart_year, use_container_width=True)

    st.divider()

    by_symbol = (
        rg_raw.groupby("symbol", as_index=False)
        .agg(
            trades       =("realized_pnl", "count"),
            proceeds     =("proceeds",     "sum"),
            cost_basis   =("cost_basis",   "sum"),
            realized_pnl =("realized_pnl", "sum"),
            avg_hold_days=("holding_days", "mean"),
        )
        .assign(pnl_pct=lambda d: d["realized_pnl"] / d["cost_basis"].replace(0, float("nan")) * 100)
        .sort_values("realized_pnl")
    )

    by_symbol.columns = ["Symbol", "Trades", "Proceeds", "Cost Basis", "Realized P/L", "Avg Hold Days", "P/L %"]
    st.dataframe(
        by_symbol,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Proceeds":      st.column_config.NumberColumn(format="$%.0f"),
            "Cost Basis":    st.column_config.NumberColumn(format="$%.0f"),
            "Realized P/L":  st.column_config.NumberColumn(format="$%.0f"),
            "Avg Hold Days": st.column_config.NumberColumn(format="%.0f"),
            "P/L %":         st.column_config.NumberColumn(format="%.1f%%"),
        },
    )

st.divider()


# ── Section 5: Holding Period Risk ───────────────────────────────────────────

st.header("Holding Period Risk")
st.caption("Open lots only — equity positions, bonds excluded. Buckets by days since purchase.")

_lot_where5 = "AND ol.account_id_key = %(acct)s" if account_filter else ""

lots5 = pd.DataFrame(query(f"""
    SELECT
        ol.symbol,
        (CURRENT_DATE - ol.buy_date)                                    AS days_held,
        (ol.quantity * p.market_value / NULLIF(p.quantity, 0))::float   AS current_value,
        ol.cost_basis::float                                             AS cost_basis
    FROM open_lots ol
    JOIN (
        SELECT account_id_key, symbol, market_value, quantity
        FROM positions
        WHERE (account_id_key, fetched_at) IN (
            SELECT account_id_key, MAX(fetched_at) FROM positions GROUP BY account_id_key
        )
          AND security_type = 'EQ'
    ) p ON p.account_id_key = ol.account_id_key AND p.symbol = ol.symbol
    WHERE 1=1 {_lot_where5}
""", _params))

if lots5.empty:
    st.info("No open lot data available.")
else:
    def _bucket(days):
        if days < 7:
            return "< 1 week"
        if days < 30:
            return "1 week – 1 month"
        if days < 365:
            return "1 month – 1 year"
        return "> 1 year"

    BUCKET_ORDER = ["< 1 week", "1 week – 1 month", "1 month – 1 year", "> 1 year"]

    lots5["bucket"]  = lots5["days_held"].apply(_bucket)
    total_lot_val    = lots5["current_value"].sum()

    avg_days = (lots5["days_held"] * lots5["current_value"]).sum() / total_lot_val if total_lot_val else 0
    lt_pct   = lots5.loc[lots5["days_held"] >= 365, "current_value"].sum() / total_lot_val * 100 if total_lot_val else 0

    k1, k2 = st.columns(2)
    k1.metric("Avg Holding Period (value-weighted)", f"{avg_days:.0f} days",
              help="Each lot's days held weighted by its current market value")
    k2.metric("Long-Term Holdings (> 1 year)", f"{lt_pct:.1f}%",
              help="% of open lot value held longer than 1 year")

    st.divider()

    by_bucket = (
        lots5.groupby("bucket", as_index=False)
        .agg(lots=("current_value", "count"), current_value=("current_value", "sum"))
        .assign(pct=lambda d: d["current_value"] / total_lot_val * 100)
    )
    by_bucket["bucket"] = pd.Categorical(by_bucket["bucket"], categories=BUCKET_ORDER, ordered=True)
    by_bucket = by_bucket.sort_values("bucket")

    chart_hp = (
        alt.Chart(by_bucket)
        .mark_bar()
        .encode(
            x=alt.X("current_value:Q", title="Current Value ($)", axis=alt.Axis(format="$,.0f")),
            y=alt.Y("bucket:N", sort=BUCKET_ORDER, title=None),
            tooltip=[
                alt.Tooltip("bucket:N",        title="Bucket"),
                alt.Tooltip("lots:Q",          title="# Lots"),
                alt.Tooltip("current_value:Q", title="Current Value", format="$,.0f"),
                alt.Tooltip("pct:Q",           title="% of Lots",     format=".1f"),
            ],
        )
        .properties(height=180)
    )
    st.altair_chart(chart_hp, use_container_width=True)

    by_bucket.columns = ["Holding Period", "# Lots", "Current Value", "% of Lots"]
    st.dataframe(
        by_bucket,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Current Value": st.column_config.NumberColumn(format="$%.0f"),
            "% of Lots":     st.column_config.NumberColumn(format="%.1f%%"),
        },
    )
