import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import query, scalar

st.set_page_config(page_title="Portfolio Overview — Fifth Dragon Capital", layout="wide")
st.title("Portfolio Overview")


# ── account filter (sidebar) ───────────────────────────────────────────────────

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

selected_label = st.sidebar.selectbox("Account", list(account_options.keys()))
account_filter = account_options[selected_label]

_where  = "AND account_id_key = %(acct)s" if account_filter else ""
_params = {"acct": account_filter} if account_filter else {}


# ── data ──────────────────────────────────────────────────────────────────────

totals = query(f"""
    SELECT
        round(sum(cost_basis)::numeric, 2)      AS cost_basis,
        round(sum(market_value)::numeric, 2)    AS market_value,
        round(sum(unrealized_pnl)::numeric, 2)  AS unrealized_pnl,
        max(as_of)                              AS as_of
    FROM mv_unrealized_pnl
    WHERE 1=1 {_where}
""", _params)[0]

cash_row = query(f"""
    SELECT
        round(sum(cash_balance)::numeric, 2)        AS cash_balance,
        round(sum(total_account_value)::numeric, 2) AS total_account_value
    FROM balances
    WHERE fetched_at = (SELECT MAX(fetched_at) FROM balances)
    {_where}
""", _params)[0]

# Daily return is portfolio-wide — mv_portfolio_timeseries has no per-account breakdown
daily_return = scalar("""
    SELECT daily_return_pct FROM mv_portfolio_timeseries ORDER BY date DESC LIMIT 1
""")

sector_df = pd.DataFrame(query(f"""
    SELECT sector,
           round(sum(market_value)::numeric, 2)     AS market_value,
           round(sum(pct_of_portfolio)::numeric, 2) AS pct
    FROM mv_allocations
    WHERE 1=1 {_where}
    GROUP BY sector
    ORDER BY market_value DESC
""", _params))

asset_df = pd.DataFrame(query(f"""
    SELECT asset_class,
           round(sum(market_value)::numeric, 2)     AS market_value,
           round(sum(pct_of_portfolio)::numeric, 2) AS pct
    FROM mv_allocations
    WHERE 1=1 {_where}
    GROUP BY asset_class
    ORDER BY market_value DESC
""", _params))

positions_df = pd.DataFrame(query(f"""
    SELECT
        a.symbol,
        a.sector,
        a.asset_class,
        round(sum(a.quantity)::numeric, 4)           AS quantity,
        round(sum(a.cost_basis)::numeric, 2)         AS cost_basis,
        round(sum(a.market_value)::numeric, 2)       AS market_value,
        round(sum(a.unrealized_pnl)::numeric, 2)     AS unrealized_pnl,
        round(
            sum(a.unrealized_pnl) / nullif(sum(a.cost_basis), 0) * 100
        , 2)                                         AS pnl_pct,
        round(sum(a.pct_of_portfolio)::numeric, 2)   AS pct_of_portfolio
    FROM mv_allocations a
    WHERE 1=1 {_where}
    GROUP BY a.symbol, a.sector, a.asset_class
    ORDER BY market_value DESC
""", _params))


# ── KPIs ──────────────────────────────────────────────────────────────────────

as_of = totals.get("as_of")
if as_of:
    st.caption(f"As of {as_of.strftime('%Y-%m-%d %H:%M ET')}")

mv       = float(totals.get("market_value")          or 0)
cost     = float(totals.get("cost_basis")            or 0)
upnl     = float(totals.get("unrealized_pnl")        or 0)
cash     = float(cash_row.get("cash_balance")        or 0)
total_av = float(cash_row.get("total_account_value") or 0)
upnl_pct = upnl / cost * 100 if cost else 0

delta_label = None
if daily_return is not None:
    suffix = " (all accounts)" if account_filter else ""
    delta_label = f"{float(daily_return):.2f}% today{suffix}"

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Account Value", f"${total_av:,.0f}", delta=delta_label)
c2.metric("Invested (Market Value)", f"${mv:,.0f}")
c3.metric("Cash Balance", f"${cash:,.0f}")
c4.metric("Unrealized P/L", f"${upnl:,.0f}")
c5.metric("Unrealized P/L %", f"{upnl_pct:.2f}%")

st.divider()


# ── Allocation charts ──────────────────────────────────────────────────────────

def donut_chart(df, theta_col, color_col, label):
    return (
        alt.Chart(df)
        .mark_arc(innerRadius=60)
        .encode(
            theta=alt.Theta(f"{theta_col}:Q"),
            color=alt.Color(f"{color_col}:N", legend=alt.Legend(title=label)),
            tooltip=[
                alt.Tooltip(f"{color_col}:N", title=label),
                alt.Tooltip(f"{theta_col}:Q", title="Market Value ($)", format=",.0f"),
                alt.Tooltip("pct:Q", title="% of Portfolio", format=".1f"),
            ],
        )
        .properties(height=300)
    )

col_l, col_r = st.columns(2)
with col_l:
    st.subheader("By Sector")
    if not sector_df.empty:
        st.altair_chart(donut_chart(sector_df, "market_value", "sector", "Sector"),
                        use_container_width=True)

with col_r:
    st.subheader("By Asset Class")
    if not asset_df.empty:
        st.altair_chart(donut_chart(asset_df, "market_value", "asset_class", "Asset Class"),
                        use_container_width=True)

st.divider()


# ── Positions table ────────────────────────────────────────────────────────────

st.subheader("Positions")

if not positions_df.empty:
    display = positions_df.copy()
    display["market_value"]     = display["market_value"].apply(lambda x: f"${float(x):,.0f}")
    display["cost_basis"]       = display["cost_basis"].apply(lambda x: f"${float(x):,.0f}")
    display["unrealized_pnl"]   = display["unrealized_pnl"].apply(lambda x: f"${float(x):,.0f}")
    display["pnl_pct"]          = display["pnl_pct"].apply(
        lambda x: f"{float(x):.1f}%" if x is not None else "—"
    )
    display["pct_of_portfolio"] = display["pct_of_portfolio"].apply(lambda x: f"{float(x):.1f}%")
    display.columns = [
        "Symbol", "Sector", "Asset Class", "Quantity",
        "Cost Basis", "Market Value", "Unrealized P/L", "P/L %", "% Portfolio",
    ]
    st.dataframe(display, use_container_width=True, hide_index=True)
else:
    st.info("No positions found for the selected account.")
