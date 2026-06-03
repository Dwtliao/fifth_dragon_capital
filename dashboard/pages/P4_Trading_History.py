import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import get_connection, query

st.set_page_config(page_title="Trading History — Fifth Dragon Capital", layout="wide")
st.title("Trading History")


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

f1, f2, f3 = st.columns([3, 2, 3])
with f1:
    sel_account = st.selectbox("Account", list(acct_options.keys()))
with f2:
    sel_period  = st.selectbox("Period", ["YTD", "1 Year", "3 Years", "All"], index=3)
with f3:
    sym_search  = st.text_input("Symbol filter", placeholder="e.g. AAPL  (leave blank for all)")

account_filter = acct_options[sel_account]
_aw  = "AND l.account_id_key = %(acct)s" if account_filter else ""
_awp = {"acct": account_filter}           if account_filter else {}
_rw  = "AND rg.account_id_key = %(acct)s" if account_filter else ""

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

_sw  = "AND symbol ILIKE %(sym)s" if sym_search.strip() else ""
_swp = {"sym": f"%{sym_search.strip()}%"} if sym_search.strip() else {}


# ── tabs ──────────────────────────────────────────────────────────────────────

tab_pnl, tab_trades, tab_ledger = st.tabs(["P/L Summary", "Trades", "Ledger"])


# ══ P/L Summary ═══════════════════════════════════════════════════════════════

with tab_pnl:

    # Monthly P/L heatmap ─────────────────────────────────────────────────────
    st.subheader("Monthly Realized P/L Heatmap")

    heatmap_raw = pd.DataFrame(query(f"""
        SELECT
            EXTRACT(year  FROM rg.sell_date)::int AS year,
            EXTRACT(month FROM rg.sell_date)::int AS month,
            SUM(rg.realized_pnl)::float           AS realized_pnl
        FROM realized_gains rg
        WHERE rg.sell_date >= %(cutoff)s {_rw} {_sw.replace('AND symbol', 'AND rg.symbol') if _sw else ''}
        GROUP BY 1, 2
        ORDER BY 1, 2
    """, {"cutoff": cutoff_s, **_awp, **_swp}))

    if not heatmap_raw.empty:
        MONTH_ABBR = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
                      7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
        heatmap_raw["month_name"] = heatmap_raw["month"].map(MONTH_ABBR)
        max_abs = heatmap_raw["realized_pnl"].abs().max()

        heatmap_chart = (
            alt.Chart(heatmap_raw)
            .mark_rect(stroke="white", strokeWidth=1)
            .encode(
                x=alt.X("month_name:O", sort=list(MONTH_ABBR.values()), title="Month"),
                y=alt.Y("year:O",       sort="descending",              title="Year"),
                color=alt.Color("realized_pnl:Q",
                    scale=alt.Scale(scheme="redyellowgreen",
                                    domain=[-max_abs, max_abs], domainMid=0),
                    legend=alt.Legend(title="P/L ($)")),
                tooltip=[
                    alt.Tooltip("year:O",          title="Year"),
                    alt.Tooltip("month_name:O",     title="Month"),
                    alt.Tooltip("realized_pnl:Q",   title="Realized P/L ($)", format=",.0f"),
                ],
            )
            .properties(height=max(120, len(heatmap_raw["year"].unique()) * 40))
        )

        text_layer = (
            alt.Chart(heatmap_raw)
            .mark_text(fontSize=11)
            .encode(
                x=alt.X("month_name:O", sort=list(MONTH_ABBR.values())),
                y=alt.Y("year:O",       sort="descending"),
                text=alt.Text("realized_pnl:Q", format="$,.0f"),
                color=alt.condition(
                    alt.datum.realized_pnl > max_abs * 0.4,
                    alt.value("white"), alt.value("black")
                ),
            )
        )
        st.altair_chart(heatmap_chart + text_layer, use_container_width=True)
    else:
        st.info("No realized gains data for selected period.")

    st.divider()

    # Cash flow + income charts ────────────────────────────────────────────────
    col_cf, col_inc = st.columns(2)

    with col_cf:
        st.subheader("Net Cash Flow by Month")
        cf_raw = pd.DataFrame(query(f"""
            SELECT
                date_trunc('month', event_timestamp)::date AS month,
                SUM(net_amount)::float                      AS net_cash
            FROM ledger l
            WHERE event_timestamp >= %(cutoff)s
              AND event_type IN ('deposit','withdrawal','transfer')
              {_aw}
            GROUP BY 1 ORDER BY 1
        """, {"cutoff": cutoff_s, **_awp}))

        if not cf_raw.empty:
            cf_raw["month"] = pd.to_datetime(cf_raw["month"])
            cf_raw = cf_raw[cf_raw["net_cash"] != 0]  # drop offsetting transfer months

        if not cf_raw.empty:
            cf_raw["color"] = cf_raw["net_cash"].apply(lambda x: "inflow" if x >= 0 else "outflow")
            st.altair_chart(
                alt.Chart(cf_raw)
                .mark_bar()
                .encode(
                    x=alt.X("month:T", title=None,
                            axis=alt.Axis(format="%b %Y", labelAngle=-45)),
                    y=alt.Y("net_cash:Q", title="Net Cash ($)", axis=alt.Axis(format="$,.0f")),
                    color=alt.Color("color:N", scale=alt.Scale(
                        domain=["inflow","outflow"], range=["#2ca02c","#d62728"]
                    ), legend=None),
                    tooltip=[
                        alt.Tooltip("month:T",    title="Month", format="%b %Y"),
                        alt.Tooltip("net_cash:Q", title="Net Cash ($)", format="$,.0f"),
                    ],
                )
                .properties(height=220),
                use_container_width=True,
            )
        else:
            st.info("No external cash activity (deposits/withdrawals) in this period.")

    with col_inc:
        st.subheader("Dividend & Interest Income")
        inc_raw = pd.DataFrame(query(f"""
            SELECT
                date_trunc('month', event_timestamp)::date AS month,
                event_type,
                SUM(net_amount)::float                      AS amount
            FROM ledger l
            WHERE event_timestamp >= %(cutoff)s
              AND event_type IN ('dividend','dividend_qualified','interest')
              AND net_amount > 0
              {_aw}
            GROUP BY 1, 2 ORDER BY 1
        """, {"cutoff": cutoff_s, **_awp}))

        if not inc_raw.empty:
            inc_raw["month"] = pd.to_datetime(inc_raw["month"])
            st.altair_chart(
                alt.Chart(inc_raw)
                .mark_bar()
                .encode(
                    x=alt.X("month:T", title=None,
                            axis=alt.Axis(format="%b %Y", labelAngle=-45)),
                    y=alt.Y("amount:Q", title="Income ($)", stack=True,
                            axis=alt.Axis(format="$,.0f")),
                    color=alt.Color("event_type:N", title="Type"),
                    tooltip=[
                        alt.Tooltip("month:T",      title="Month",      format="%b %Y"),
                        alt.Tooltip("event_type:N", title="Type"),
                        alt.Tooltip("amount:Q",     title="Amount ($)", format="$,.2f"),
                    ],
                )
                .properties(height=220),
                use_container_width=True,
            )
        else:
            st.info("No dividend or interest income in this period.")


# ══ Trades ════════════════════════════════════════════════════════════════════

with tab_trades:

    trades_raw = pd.DataFrame(query(f"""
        SELECT
            rg.id, rg.account_id_key, rg.symbol,
            rg.buy_date, rg.sell_date,
            rg.quantity::float, rg.buy_price::float, rg.sell_price::float,
            rg.cost_basis::float, rg.proceeds::float,
            rg.realized_pnl::float, rg.holding_days, rg.term,
            COALESCE(
                string_agg(tt.tag, ', ' ORDER BY tt.tag), '—'
            ) AS tags
        FROM realized_gains rg
        LEFT JOIN trade_tags tt ON tt.realized_gain_id = rg.id
        WHERE rg.sell_date >= %(cutoff)s {_rw} {_sw.replace('AND symbol', 'AND rg.symbol') if _sw else ''}
        GROUP BY rg.id, rg.account_id_key, rg.symbol,
                 rg.buy_date, rg.sell_date, rg.quantity, rg.buy_price, rg.sell_price,
                 rg.cost_basis, rg.proceeds, rg.realized_pnl, rg.holding_days, rg.term
        ORDER BY rg.sell_date DESC
    """, {"cutoff": cutoff_s, **_awp, **_swp}))

    if trades_raw.empty:
        st.info("No closed trades in selected period.")
    else:
        trades_raw["pnl_pct"] = trades_raw["realized_pnl"] / trades_raw["cost_basis"] * 100

        # KPIs ─────────────────────────────────────────────────────────────────
        wins     = trades_raw[trades_raw["realized_pnl"] > 0]
        losses   = trades_raw[trades_raw["realized_pnl"] < 0]
        win_rate = len(wins) / len(trades_raw) * 100

        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Total Trades",  len(trades_raw))
        k2.metric("Win Rate",      f"{win_rate:.0f}%")
        k3.metric("Avg Win",       f"${wins['realized_pnl'].mean():,.0f}"   if not wins.empty   else "—")
        k4.metric("Avg Loss",      f"${losses['realized_pnl'].mean():,.0f}" if not losses.empty else "—")
        k5.metric("Total Realized P/L", f"${trades_raw['realized_pnl'].sum():,.0f}")

        st.divider()

        # Scatterplot ──────────────────────────────────────────────────────────
        st.subheader("Return % vs Holding Days")

        scatter_df = trades_raw.dropna(subset=["holding_days", "pnl_pct"]).copy()
        scatter_df["color"] = scatter_df["realized_pnl"].apply(
            lambda x: "Win" if x > 0 else "Loss"
        )

        st.altair_chart(
            alt.Chart(scatter_df)
            .mark_circle(opacity=0.7)
            .encode(
                x=alt.X("holding_days:Q", title="Holding Days"),
                y=alt.Y("pnl_pct:Q",      title="Return %"),
                color=alt.Color("color:N", scale=alt.Scale(
                    domain=["Win","Loss"], range=["#2ca02c","#d62728"]
                )),
                size=alt.Size("proceeds:Q", title="Proceeds ($)",
                              scale=alt.Scale(range=[30, 400]), legend=None),
                tooltip=[
                    alt.Tooltip("symbol:N",       title="Symbol"),
                    alt.Tooltip("sell_date:T",    title="Sell Date"),
                    alt.Tooltip("holding_days:Q", title="Days Held"),
                    alt.Tooltip("pnl_pct:Q",      title="Return %",     format="+.1f"),
                    alt.Tooltip("realized_pnl:Q", title="P/L ($)",      format="$,.0f"),
                    alt.Tooltip("proceeds:Q",      title="Proceeds ($)", format="$,.0f"),
                    alt.Tooltip("term:N",          title="Term"),
                ],
            )
            .properties(height=280)
            + alt.Chart(pd.DataFrame({"y":[0]})).mark_rule(
                color="gray", strokeDash=[4,4], opacity=0.5
            ).encode(y="y:Q"),
            use_container_width=True,
        )

        st.divider()

        # Trades table ─────────────────────────────────────────────────────────
        st.subheader("Closed Trades")

        display = trades_raw[[
            "sell_date","symbol","holding_days","term",
            "quantity","buy_price","sell_price",
            "cost_basis","proceeds","realized_pnl","pnl_pct","tags"
        ]].copy()
        display["sell_date"]    = pd.to_datetime(display["sell_date"]).dt.strftime("%Y-%m-%d")
        display["buy_price"]    = display["buy_price"].apply(lambda x: f"${x:,.2f}")
        display["sell_price"]   = display["sell_price"].apply(lambda x: f"${x:,.2f}")
        display["cost_basis"]   = display["cost_basis"].apply(lambda x: f"${x:,.0f}")
        display["proceeds"]     = display["proceeds"].apply(lambda x: f"${x:,.0f}")
        display["realized_pnl"] = display["realized_pnl"].apply(lambda x: f"${x:,.0f}")
        display["pnl_pct"]      = display["pnl_pct"].apply(lambda x: f"{x:+.1f}%")
        display["quantity"]     = display["quantity"].apply(
            lambda x: f"{x:,.0f}" if x == int(x) else f"{x:,.4f}"
        )
        display.columns = [
            "Sell Date","Symbol","Days","Term","Qty",
            "Buy Price","Sell Price","Cost Basis","Proceeds","P/L ($)","P/L %","Tags"
        ]
        st.dataframe(display, use_container_width=True, hide_index=True, height=350)

        st.divider()

        # Strategy tag form ────────────────────────────────────────────────────
        st.subheader("Tag a Trade")

        trade_options = {
            f"{r['symbol']} — sold {r['sell_date']} | P/L ${r['realized_pnl']:,.0f}": r["id"]
            for _, r in trades_raw.iterrows()
        }

        existing_tags = [r["tag"] for r in query(
            "SELECT DISTINCT tag FROM trade_tags ORDER BY tag"
        )]

        with st.form("tag_form"):
            t1, t2 = st.columns([3, 2])
            sel_trade = t1.selectbox("Trade", list(trade_options.keys()))
            tag_input = t2.text_input("Tag",
                placeholder="e.g. momentum, earnings play, sector rotation")
            notes_input = st.text_input("Notes (optional)")
            if existing_tags:
                st.caption(f"Existing tags: {', '.join(existing_tags)}")
            save_tag = st.form_submit_button("Save Tag")

        if save_tag:
            if not tag_input.strip():
                st.error("Tag cannot be blank.")
            else:
                trade_id    = trade_options[sel_trade]
                symbol_val  = trades_raw.loc[trades_raw["id"] == trade_id, "symbol"].iloc[0]
                acct_val    = trades_raw.loc[trades_raw["id"] == trade_id, "account_id_key"].iloc[0]
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO trade_tags
                                (account_id_key, symbol, realized_gain_id, tag, notes, updated_at)
                            VALUES (%s, %s, %s, %s, %s, NOW())
                            ON CONFLICT (realized_gain_id, tag) DO UPDATE SET
                                notes      = EXCLUDED.notes,
                                updated_at = NOW()
                        """, (acct_val, symbol_val, trade_id,
                              tag_input.strip(), notes_input.strip() or None))
                st.success(f"Tagged **{symbol_val}** as **{tag_input.strip()}**.")
                st.rerun()


# ══ Ledger ════════════════════════════════════════════════════════════════════

with tab_ledger:

    lf1, lf2 = st.columns([3, 2])
    with lf1:
        all_types = ["buy","sell","dividend","dividend_qualified","interest",
                     "fee","transfer","deposit","withdrawal","split","redemption"]
        sel_types = st.multiselect("Event types", all_types,
                                   default=["buy","sell","dividend","dividend_qualified",
                                            "interest","fee"])
    with lf2:
        ledger_limit = st.selectbox("Show", [100, 250, 500, 1000], index=0)

    type_filter = ""
    type_params: dict = {}
    if sel_types:
        placeholders = ", ".join([f"%(t{i})s" for i in range(len(sel_types))])
        type_filter  = f"AND l.event_type IN ({placeholders})"
        type_params  = {f"t{i}": v for i, v in enumerate(sel_types)}

    sym_ledger = _sw.replace("AND symbol", "AND l.symbol")

    ledger_df = pd.DataFrame(query(f"""
        SELECT
            l.event_timestamp::date  AS date,
            a.account_id,
            l.symbol,
            l.event_type,
            l.quantity::float,
            l.price::float,
            l.net_amount::float,
            l.fee::float
        FROM ledger l
        JOIN accounts a USING (account_id_key)
        WHERE l.event_timestamp >= %(cutoff)s
          {_aw} {type_filter} {sym_ledger}
        ORDER BY l.event_timestamp DESC
        LIMIT %(lim)s
    """, {"cutoff": cutoff_s, **_awp, **type_params, **_swp, "lim": ledger_limit}))

    if ledger_df.empty:
        st.info("No ledger entries match the current filters.")
    else:
        st.caption(f"Showing {len(ledger_df):,} rows (limit {ledger_limit})")
        ledger_display = ledger_df.copy()
        ledger_display["date"]       = pd.to_datetime(ledger_display["date"]).dt.strftime("%Y-%m-%d")
        ledger_display["net_amount"] = ledger_display["net_amount"].apply(
            lambda x: f"${x:,.2f}" if x is not None else "—"
        )
        ledger_display["price"]      = ledger_display["price"].apply(
            lambda x: f"${x:,.4f}" if x else "—"
        )
        ledger_display["quantity"]   = ledger_display["quantity"].apply(
            lambda x: f"{x:,.4f}" if x else "—"
        )
        ledger_display["fee"]        = ledger_display["fee"].apply(
            lambda x: f"${x:,.2f}" if x else "—"
        )
        ledger_display.columns = [
            "Date","Account","Symbol","Type","Qty","Price","Net Amount","Fee"
        ]
        st.dataframe(ledger_display, use_container_width=True,
                     hide_index=True, height=500)
