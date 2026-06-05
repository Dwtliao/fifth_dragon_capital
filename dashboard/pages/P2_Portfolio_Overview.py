import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import query, scalar

st.set_page_config(page_title="Portfolio Overview — Fifth Dragon Capital", layout="wide")
st.title("Portfolio Overview")


# ── sidebar filters ────────────────────────────────────────────────────────────

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

st.sidebar.divider()
st.sidebar.markdown("**Filter Positions**")


# ── fetch positions (account-filtered) ────────────────────────────────────────

positions_raw = pd.DataFrame(query(f"""
    SELECT account_id_key, symbol, sector, asset_class,
           quantity::float         AS quantity,
           cost_basis::float       AS cost_basis,
           market_value::float     AS market_value,
           unrealized_pnl::float   AS unrealized_pnl,
           pct_of_portfolio::float AS pct_of_portfolio
    FROM mv_allocations
    WHERE 1=1 {_where}
    ORDER BY market_value DESC
""", _params))


# ── position filter options (built from fetched data) ─────────────────────────

if not positions_raw.empty:
    sel_symbols     = st.sidebar.multiselect("Symbol",      sorted(positions_raw["symbol"].unique()))
    sel_sectors     = st.sidebar.multiselect("Sector",      sorted(positions_raw["sector"].unique()))
    sel_asset_class = st.sidebar.multiselect("Asset Class", sorted(positions_raw["asset_class"].unique()))
else:
    sel_symbols = sel_sectors = sel_asset_class = []

# Apply position filters
filtered = positions_raw.copy()
if sel_symbols:
    filtered = filtered[filtered["symbol"].isin(sel_symbols)]
if sel_sectors:
    filtered = filtered[filtered["sector"].isin(sel_sectors)]
if sel_asset_class:
    filtered = filtered[filtered["asset_class"].isin(sel_asset_class)]

filters_active = bool(sel_symbols or sel_sectors or sel_asset_class)


# ── aggregate for KPIs and charts ─────────────────────────────────────────────

mv_filtered   = filtered["market_value"].sum()
cost_filtered = filtered["cost_basis"].sum()
upnl_filtered = filtered["unrealized_pnl"].sum()
upnl_pct      = upnl_filtered / cost_filtered * 100 if cost_filtered else 0

sector_chart_df = (
    filtered.groupby("sector", as_index=False)
    .agg(market_value=("market_value", "sum"), pct=("pct_of_portfolio", "sum"))
    .sort_values("market_value", ascending=False)
)
asset_chart_df = (
    filtered.groupby("asset_class", as_index=False)
    .agg(market_value=("market_value", "sum"), pct=("pct_of_portfolio", "sum"))
    .sort_values("market_value", ascending=False)
)

positions_display = (
    filtered
    .groupby(["symbol", "sector", "asset_class"], as_index=False)
    .agg(
        quantity      =("quantity",       "sum"),
        cost_basis    =("cost_basis",     "sum"),
        market_value  =("market_value",   "sum"),
        unrealized_pnl=("unrealized_pnl", "sum"),
        pct_of_portfolio=("pct_of_portfolio", "sum"),
    )
    .assign(pnl_pct=lambda d: d["unrealized_pnl"] / d["cost_basis"].replace(0, float("nan")) * 100)
    .sort_values("market_value", ascending=False)
)


# ── cash / total account value (always account-level, not position-filtered) ──

cash_row = query(f"""
    SELECT
        round(sum(cash_available_for_invest)::numeric, 2) AS net_cash,
        round(sum(total_account_value)::numeric, 2) AS total_account_value
    FROM balances
    WHERE fetched_at = (SELECT MAX(fetched_at) FROM balances)
    {_where}
""", _params)[0]

daily_return = scalar("""
    SELECT daily_return_pct FROM mv_portfolio_timeseries ORDER BY date DESC LIMIT 1
""")

as_of_ts = scalar(f"""
    SELECT max(as_of) FROM mv_unrealized_pnl WHERE 1=1 {_where}
""", _params)


# ── KPIs ──────────────────────────────────────────────────────────────────────

if as_of_ts:
    st.caption(f"As of {as_of_ts.strftime('%Y-%m-%d %H:%M ET')}")

if filters_active:
    st.info(
        f"Showing filtered positions: "
        + (f"symbol={sel_symbols}  " if sel_symbols else "")
        + (f"sector={sel_sectors}  " if sel_sectors else "")
        + (f"asset class={sel_asset_class}" if sel_asset_class else "")
    )

cash     = float(cash_row.get("net_cash")             or 0)
total_av = float(cash_row.get("total_account_value") or 0)

delta_label = None
if daily_return is not None:
    suffix = " (all accounts)" if account_filter else ""
    delta_label = f"{float(daily_return):.2f}% today{suffix}"

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Account Value", f"${total_av:,.0f}", delta=delta_label,
          help="Cash + invested value for selected account. Not affected by position filters.")
c2.metric("Invested (Market Value)", f"${mv_filtered:,.0f}")
c3.metric("Cash", f"${cash:,.0f}",
          help="Net cash including money market funds. Not affected by position filters.")
c4.metric("Unrealized P/L", f"${upnl_filtered:,.0f}")
c5.metric("Unrealized P/L %", f"{upnl_pct:.2f}%")

st.divider()


# ── allocation charts (respond to position filters) ───────────────────────────

def donut_chart(df, theta_col, color_col, label):
    df = df.copy()
    # Only label slices >= 3% to avoid clutter on small slivers
    df["slice_label"] = df.apply(
        lambda r: f"{r['pct']:.1f}%  ${r[theta_col]:,.0f}"
        if r["pct"] >= 3 else "",
        axis=1,
    )

    arc = (
        alt.Chart(df)
        .mark_arc(innerRadius=60)
        .encode(
            theta=alt.Theta(f"{theta_col}:Q"),
            color=alt.Color(f"{color_col}:N", legend=alt.Legend(title=label)),
            tooltip=[
                alt.Tooltip(f"{color_col}:N", title=label),
                alt.Tooltip(f"{theta_col}:Q", title="Market Value ($)", format=",.0f"),
                alt.Tooltip("pct:Q",          title="% of Portfolio",   format=".1f"),
            ],
        )
    )

    text = (
        alt.Chart(df)
        .mark_text(radius=155, size=11)
        .encode(
            theta=alt.Theta(f"{theta_col}:Q", stack=True),
            text=alt.Text("slice_label:N"),
        )
    )

    return (arc + text).properties(height=320)

def summary_table(df, label_col):
    display = df[[label_col, "market_value", "pct"]].copy()
    display["market_value"] = display["market_value"].apply(lambda x: f"${x:,.0f}")
    display["pct"]          = display["pct"].apply(lambda x: f"{x:.1f}%")
    display.columns         = [label_col.replace("_", " ").title(), "Market Value", "% Portfolio"]
    st.dataframe(display, use_container_width=True, hide_index=True)


col_l, col_r = st.columns(2)
with col_l:
    st.subheader("By Sector")
    if not sector_chart_df.empty:
        st.altair_chart(donut_chart(sector_chart_df, "market_value", "sector", "Sector"),
                        use_container_width=True)
        summary_table(sector_chart_df, "sector")

with col_r:
    st.subheader("By Asset Class")
    if not asset_chart_df.empty:
        st.altair_chart(donut_chart(asset_chart_df, "market_value", "asset_class", "Asset Class"),
                        use_container_width=True)
        summary_table(asset_chart_df, "asset_class")

st.divider()


# ── positions table ────────────────────────────────────────────────────────────

st.subheader("Positions")

if not positions_display.empty:
    display = positions_display.copy()
    display["market_value"]     = display["market_value"].apply(lambda x: f"${x:,.0f}")
    display["cost_basis"]       = display["cost_basis"].apply(lambda x: f"${x:,.0f}")
    display["unrealized_pnl"]   = display["unrealized_pnl"].apply(lambda x: f"${x:,.0f}")
    display["pnl_pct"]          = display["pnl_pct"].apply(
        lambda x: f"{x:.1f}%" if pd.notna(x) else "—"
    )
    display["pct_of_portfolio"] = display["pct_of_portfolio"].apply(lambda x: f"{x:.1f}%")
    display["quantity"]         = display["quantity"].apply(
        lambda x: f"{x:,.0f}" if x == int(x) else f"{x:,.4f}"
    )
    display.columns = [
        "Symbol", "Sector", "Asset Class", "Quantity",
        "Cost Basis", "Market Value", "Unrealized P/L", "P/L %", "% Portfolio",
    ]
    st.dataframe(display, use_container_width=True, hide_index=True)
else:
    st.info("No positions match the current filters.")


# ── lot detail (symbols with multiple open buy lots) ──────────────────────────

_lot_where = "AND ol.account_id_key = %(acct)s" if account_filter else ""

lots_df = pd.DataFrame(query(f"""
    SELECT
        ol.account_id_key,
        a.account_id,
        ol.symbol,
        ol.buy_date,
        ol.buy_price::float                                         AS buy_price,
        ol.quantity::float                                          AS quantity,
        ol.cost_basis::float                                        AS cost_basis,
        (p.market_value / NULLIF(p.quantity, 0))::float            AS current_price,
        (ol.quantity * p.market_value / NULLIF(p.quantity, 0))::float AS current_value,
        (ol.quantity * p.market_value / NULLIF(p.quantity, 0)
            - ol.cost_basis)::float                                AS unrealized_pnl,
        (CURRENT_DATE - ol.buy_date)                               AS days_held
    FROM open_lots ol
    JOIN accounts a USING (account_id_key)
    JOIN (
        SELECT account_id_key, symbol, market_value, quantity, security_type
        FROM positions
        WHERE fetched_at = (SELECT MAX(fetched_at) FROM positions)
    ) p ON p.account_id_key = ol.account_id_key AND p.symbol = ol.symbol
    WHERE p.security_type = 'EQ'  -- bonds use face-value quantities; exclude to avoid misleading numbers
      {_lot_where}
    ORDER BY ol.symbol, ol.buy_date
""", _params))

# Position quantities from E*TRADE for reconciliation check
pos_qty = {}
if not lots_df.empty:
    pos_raw = query(f"""
        SELECT symbol, SUM(quantity)::float AS qty
        FROM positions
        WHERE fetched_at = (SELECT MAX(fetched_at) FROM positions)
          AND security_type = 'EQ'
          {_lot_where.replace('ol.account_id_key', 'account_id_key')}
        GROUP BY symbol
    """, _params)
    pos_qty = {r["symbol"]: r["qty"] for r in pos_raw}

if not lots_df.empty:
    lots_df["pnl_pct"] = (
        lots_df["unrealized_pnl"] / lots_df["cost_basis"].replace(0, float("nan")) * 100
    )

    # Only symbols that have >1 lot across accounts (or within the selected account)
    lot_counts = lots_df.groupby("symbol")["buy_date"].count()
    multi_lot_symbols = lot_counts[lot_counts > 1].index.tolist()
    multi_lots = lots_df[lots_df["symbol"].isin(multi_lot_symbols)].copy()

    if not multi_lots.empty:
        st.divider()
        st.subheader("Position Lot Detail")
        st.caption("Symbols with multiple purchase lots — buy prices are split-adjusted.")

        for symbol in sorted(multi_lots["symbol"].unique()):
            sym_lots = multi_lots[multi_lots["symbol"] == symbol].copy()

            # Reconcile lot total qty against actual position qty
            lot_total_qty = sym_lots["quantity"].sum()
            position_qty  = pos_qty.get(symbol, None)
            reconciled    = position_qty is None or abs(lot_total_qty - position_qty) < 0.01

            total_cost = sym_lots["cost_basis"].sum()
            total_val  = sym_lots["current_value"].sum()
            total_pnl  = sym_lots["unrealized_pnl"].sum()
            total_pnl_pct = total_pnl / total_cost * 100 if total_cost else 0

            warn = "" if reconciled else "  ⚠️ lot qty mismatch"
            label = (
                f"{symbol}  —  {len(sym_lots)} lots  |  "
                f"Cost ${total_cost:,.0f}  →  "
                f"Value ${total_val:,.0f}  |  "
                f"P/L ${total_pnl:+,.0f}  ({total_pnl_pct:+.1f}%)"
                f"{warn}"
            )
            with st.expander(label):
                if not reconciled:
                    st.warning(
                        f"Lot total ({lot_total_qty:,.0f} shares) doesn't match "
                        f"position ({position_qty:,.0f} shares). "
                        "E*TRADE likely recorded the same fill twice under different transaction IDs. "
                        "Run **Reconcile** from Pipeline Status to investigate.",
                        icon="⚠️",
                    )
                show = sym_lots[[
                    "account_id", "buy_date", "quantity",
                    "buy_price", "cost_basis",
                    "current_price", "current_value",
                    "unrealized_pnl", "pnl_pct", "days_held",
                ]].copy()

                show["buy_date"]      = pd.to_datetime(show["buy_date"]).dt.strftime("%Y-%m-%d")
                show["buy_price"]     = show["buy_price"].apply(lambda x: f"${x:,.4f}")
                show["current_price"] = show["current_price"].apply(lambda x: f"${x:,.4f}")
                show["quantity"]      = show["quantity"].apply(
                    lambda x: f"{x:,.0f}" if x == int(x) else f"{x:,.4f}"
                )
                show["cost_basis"]    = show["cost_basis"].apply(lambda x: f"${x:,.0f}")
                show["current_value"] = show["current_value"].apply(lambda x: f"${x:,.0f}")
                show["unrealized_pnl"] = show["unrealized_pnl"].apply(lambda x: f"${x:+,.0f}")
                show["pnl_pct"]       = show["pnl_pct"].apply(
                    lambda x: f"{x:+.1f}%" if pd.notna(x) else "—"
                )
                show["days_held"]     = show["days_held"].apply(lambda x: f"{int(x)}d" if pd.notna(x) else "—")

                show.columns = [
                    "Account", "Buy Date", "Qty", "Buy Price (adj)",
                    "Cost Basis", "Current Price", "Current Value",
                    "Unrealized P/L", "P/L %", "Days Held",
                ]
                st.dataframe(show, use_container_width=True, hide_index=True)
