import sys
from pathlib import Path
from datetime import date

import pandas as pd
import streamlit as st
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import query, execute

st.set_page_config(page_title="Physical Metals — Fifth Dragon Capital", layout="wide")
st.title("Physical Precious Metals")

METALS = ["gold", "silver", "platinum", "palladium"]
METAL_LABEL  = {"gold": "Gold", "silver": "Silver", "platinum": "Platinum", "palladium": "Palladium"}
METAL_TICKER = {"gold": "GC=F", "silver": "SI=F", "platinum": "PL=F", "palladium": "PA=F"}


@st.cache_data(ttl=3600)
def fetch_market_spot() -> dict:
    """Return previous-close spot price for each metal via yfinance futures."""
    result = {}
    for metal, ticker in METAL_TICKER.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if not hist.empty:
                result[metal] = float(hist["Close"].iloc[-1])
        except Exception:
            pass
    return result


# ── spot prices ────────────────────────────────────────────────────────────────

spot_rows = query("""
    SELECT DISTINCT ON (metal) metal, spot_price::float, price_date
    FROM physical_prices_pm
    ORDER BY metal, price_date DESC
""")
spot       = {r["metal"]: r["spot_price"] for r in spot_rows}
spot_dates = {r["metal"]: r["price_date"] for r in spot_rows}

market_spot = fetch_market_spot()  # previous-close prices from yfinance (cached 1h)

# ── holdings ───────────────────────────────────────────────────────────────────

raw = query("""
    SELECT id, account_name, location, metal,
           weight_oz::float, purchase_price::float, purchase_date, description
    FROM physical_holdings_pm
    ORDER BY account_name, location, metal
""")
holdings = pd.DataFrame(raw)

# ── KPI strip ──────────────────────────────────────────────────────────────────

active_spot = {**market_spot, **spot}  # DB prices override market; both are fallbacks
if not holdings.empty and active_spot:
    holdings["spot_per_oz"]     = holdings["metal"].map(active_spot)
    holdings["spot_value"]      = holdings["weight_oz"] * holdings["spot_per_oz"]
    holdings["unrealized_gain"] = holdings["spot_value"] - holdings["purchase_price"]

    total_value = holdings["spot_value"].sum()
    total_cost  = holdings["purchase_price"].sum()
    total_gain  = total_value - total_cost
    gain_pct    = total_gain / total_cost * 100 if total_cost else 0

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Spot Value",          f"${total_value:,.0f}")
    k2.metric("Total Cost",          f"${total_cost:,.0f}")
    k3.metric("Unrealized Gain/Loss", f"${total_gain:,.0f}")
    k4.metric("Return",              f"{gain_pct:.1f}%")

    st.divider()

    # ── holdings table ─────────────────────────────────────────────────────────

    st.subheader("Holdings")

    display = holdings[[
        "id", "account_name", "location", "metal", "weight_oz",
        "description", "purchase_price", "spot_per_oz", "spot_value", "unrealized_gain",
    ]].copy()
    display["gain_pct"] = display["unrealized_gain"] / display["purchase_price"].replace(0, float("nan")) * 100
    display["metal"]    = display["metal"].map(METAL_LABEL)
    display.columns     = [
        "ID", "Account", "Location", "Metal", "Weight (oz)",
        "Description", "Cost", "Spot/oz", "Spot Value", "Unrealized G/L", "G/L %",
    ]

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Weight (oz)":    st.column_config.NumberColumn(format="%.3f"),
            "Cost":           st.column_config.NumberColumn(format="$%.2f"),
            "Spot/oz":        st.column_config.NumberColumn(format="$%.2f"),
            "Spot Value":     st.column_config.NumberColumn(format="$%.2f"),
            "Unrealized G/L": st.column_config.NumberColumn(format="$%.2f"),
            "G/L %":          st.column_config.NumberColumn(format="%.1f%%"),
        },
    )

    st.divider()

    # ── by metal summary ───────────────────────────────────────────────────────

    st.subheader("By Metal")

    by_metal = (
        holdings.groupby("metal", as_index=False)
        .agg(
            weight_oz      =("weight_oz",       "sum"),
            cost           =("purchase_price",  "sum"),
            spot_value     =("spot_value",      "sum"),
            unrealized_gain=("unrealized_gain", "sum"),
        )
    )
    by_metal["gain_pct"] = by_metal["unrealized_gain"] / by_metal["cost"].replace(0, float("nan")) * 100
    by_metal["metal"]    = by_metal["metal"].map(METAL_LABEL)
    by_metal.columns     = ["Metal", "Weight (oz)", "Cost", "Spot Value", "Unrealized G/L", "G/L %"]

    st.dataframe(
        by_metal,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Weight (oz)":    st.column_config.NumberColumn(format="%.3f"),
            "Cost":           st.column_config.NumberColumn(format="$%.2f"),
            "Spot Value":     st.column_config.NumberColumn(format="$%.2f"),
            "Unrealized G/L": st.column_config.NumberColumn(format="$%.2f"),
            "G/L %":          st.column_config.NumberColumn(format="%.1f%%"),
        },
    )

elif holdings.empty:
    st.info("No physical holdings recorded yet. Add your first holding below.")

st.divider()


# ── Update Spot Prices ─────────────────────────────────────────────────────────

with st.expander("Update Spot Prices"):
    st.caption("Pre-filled from previous market close (yfinance). Override if needed, then save.")
    with st.form("spot_form"):
        cols = st.columns(4)
        inputs = {}
        for i, metal in enumerate(METALS):
            last = spot_dates.get(metal)
            # Priority: saved DB price → yfinance market close → 0
            default = float(spot.get(metal, market_spot.get(metal, 0.0)))
            source  = "DB" if metal in spot else ("market close" if metal in market_spot else "not set")
            inputs[metal] = cols[i].number_input(
                METAL_LABEL[metal],
                min_value=0.0,
                value=default,
                step=0.01,
                format="%.2f",
                help=f"Source: {source}" + (f" (last saved: {last})" if last else ""),
            )
        if st.form_submit_button("Save Prices", type="primary"):
            today = date.today()
            for metal, price in inputs.items():
                if price > 0:
                    execute("""
                        INSERT INTO physical_prices_pm (metal, price_date, spot_price)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (metal, price_date) DO UPDATE SET spot_price = EXCLUDED.spot_price
                    """, (metal, today, price))
            st.success("Spot prices saved.")
            st.rerun()


# ── Add Holding ────────────────────────────────────────────────────────────────

with st.expander("Add Holding"):
    with st.form("add_form"):
        c1, c2 = st.columns(2)
        account_name = c1.text_input("Account Name", placeholder="e.g. Home Safe")
        location     = c2.text_input("Location",     placeholder="e.g. San Francisco, CA")
        c3, c4, c5 = st.columns(3)
        metal          = c3.selectbox("Metal", METALS, format_func=lambda m: METAL_LABEL[m])
        weight_oz      = c4.number_input("Weight (oz)",              min_value=0.001, step=0.001, format="%.3f")
        purchase_price = c5.number_input("Total Purchase Price ($)", min_value=0.0,   step=0.01,  format="%.2f")
        c6, c7 = st.columns(2)
        purchase_date = c6.date_input("Purchase Date", value=date.today(), min_value=date(2000, 1, 1), max_value=date.today())
        description   = c7.text_input("Description", placeholder="e.g. 1 oz American Eagle")
        if st.form_submit_button("Add Holding", type="primary"):
            if not account_name.strip() or not location.strip():
                st.error("Account Name and Location are required.")
            else:
                execute("""
                    INSERT INTO physical_holdings_pm
                        (account_name, location, metal, weight_oz, purchase_price, purchase_date, description)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    account_name.strip(), location.strip(), metal,
                    weight_oz, purchase_price, purchase_date,
                    description.strip() or None,
                ))
                st.success(f"Added {weight_oz:.3f} oz {METAL_LABEL[metal]}.")
                st.rerun()


# ── Delete Holding ─────────────────────────────────────────────────────────────

if not holdings.empty:
    with st.expander("Delete Holding"):
        rows = query("""
            SELECT id, account_name, location, metal, weight_oz::float
            FROM physical_holdings_pm ORDER BY id
        """)
        options = {
            f"#{r['id']} — {r['account_name']} / {r['location']} / {METAL_LABEL[r['metal']]} {r['weight_oz']:.3f} oz": r["id"]
            for r in rows
        }
        chosen = st.selectbox("Select holding to remove", list(options.keys()))
        if st.button("Delete", type="secondary"):
            execute("DELETE FROM physical_holdings_pm WHERE id = %s", (options[chosen],))
            st.success("Deleted.")
            st.rerun()
