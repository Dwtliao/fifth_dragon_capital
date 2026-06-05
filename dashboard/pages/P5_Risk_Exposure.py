import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import query

st.set_page_config(page_title="Risk & Exposure — Fifth Dragon Capital", layout="wide")
st.title("Risk & Exposure")


# ── account filter (same pattern as P2/P3) ────────────────────────────────────

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


# ── Section 1: Concentration Risk ─────────────────────────────────────────────

st.header("Concentration Risk")

alloc = pd.DataFrame(query(f"""
    SELECT symbol, sector, asset_class,
           SUM(market_value)::float     AS market_value,
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

# Recompute % portfolio from filtered market values so it always sums to 100%.
total_mv = alloc["market_value"].sum()
alloc["pct_of_portfolio"] = alloc["market_value"] / total_mv * 100 if total_mv else 0

# ── KPI badges ────────────────────────────────────────────────────────────────

largest       = alloc.iloc[0]
top3_pct      = alloc.head(3)["pct_of_portfolio"].sum()
risk_pct      = alloc[~alloc["asset_class"].isin(["Cash", "Fixed Income"])]["pct_of_portfolio"].sum()

k1, k2, k3 = st.columns(3)
k1.metric(
    "Largest Single Position",
    f"{largest['symbol']}  {largest['pct_of_portfolio']:.1f}%",
    help="Symbol with highest % of total portfolio",
)
k2.metric(
    "Top 3 Positions Combined",
    f"{top3_pct:.1f}%",
    help=f"{', '.join(alloc.head(3)['symbol'].tolist())}",
)
k3.metric(
    "Risk Asset Exposure",
    f"{risk_pct:.1f}%",
    help="All assets excluding Cash and Fixed Income",
)

st.divider()

# ── concentration bar charts ──────────────────────────────────────────────────

col_left, col_right = st.columns(2)

with col_left:
    st.subheader("By Sector")
    st.caption("Risk assets only — Cash & Fixed Income excluded")

    sector_df = (
        alloc[~alloc["asset_class"].isin(["Cash", "Fixed Income"])]
        .groupby("sector", as_index=False)["pct_of_portfolio"]
        .sum()
        .sort_values("pct_of_portfolio", ascending=False)
    )

    if sector_df.empty:
        st.info("No risk asset positions.")
    else:
        chart_s = (
            alt.Chart(sector_df)
            .mark_bar()
            .encode(
                x=alt.X("pct_of_portfolio:Q", title="% of Portfolio", axis=alt.Axis(format=".1f")),
                y=alt.Y("sector:N", sort="-x", title=None),
                tooltip=[
                    alt.Tooltip("sector:N", title="Sector"),
                    alt.Tooltip("pct_of_portfolio:Q", title="% Portfolio", format=".1f"),
                ],
            )
            .properties(height=max(120, len(sector_df) * 35))
        )
        st.altair_chart(chart_s, use_container_width=True)

with col_right:
    st.subheader("By Asset Class")
    st.caption("All assets including Cash & Fixed Income")

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
                alt.Tooltip("asset_class:N", title="Asset Class"),
                alt.Tooltip("pct_of_portfolio:Q", title="% Portfolio", format=".1f"),
            ],
        )
        .properties(height=max(120, len(ac_df) * 35))
    )
    st.altair_chart(chart_a, use_container_width=True)

st.divider()

# ── thematic exposure callout ─────────────────────────────────────────────────

st.subheader("Thematic Exposure")
st.caption("Market value grouped by economic theme. Values may overlap — a position can carry multiple tags.")

CALLOUT_TAGS = ["Precious Metals", "Uranium", "Broad Energy", "Copper", "Agriculture", "Volatility"]

themes_raw = query(f"""
    SELECT t.tag,
           SUM(a.market_value)::float     AS market_value,
           SUM(a.pct_of_portfolio)::float AS pct_of_portfolio
    FROM mv_allocations a
    JOIN symbol_exposure_tags t ON t.symbol = a.symbol
    WHERE t.tag = ANY(%(tags)s)
      {'AND a.account_id_key = %(acct)s' if account_filter else ''}
    GROUP BY t.tag
    ORDER BY market_value DESC
""", {"tags": CALLOUT_TAGS, "acct": account_filter} if account_filter else {"tags": CALLOUT_TAGS})

if not themes_raw:
    st.info("No thematic exposure tags defined. Add tags in Symbol Admin → Exposure Tags.")
else:
    themes_df = pd.DataFrame(themes_raw)
    cols = st.columns(len(themes_df))
    for col, (_, row) in zip(cols, themes_df.iterrows()):
        col.metric(
            row["tag"],
            f"{row['pct_of_portfolio']:.1f}%",
            f"${row['market_value']:,.0f}",
            delta_color="off",
        )
