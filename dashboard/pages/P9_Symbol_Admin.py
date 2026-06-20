import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import get_connection, query, scalar

st.set_page_config(page_title="Symbol Admin — Fifth Dragon Capital", layout="wide")
st.title("Symbol Admin")
st.caption("Override sector and asset class for any symbol. Overrides take precedence over yfinance data.")


# ── helpers ───────────────────────────────────────────────────────────────────

def _refresh_mv():
    conn = get_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_allocations")
    finally:
        conn.close()


def _upsert_override(symbol, sector, asset_class, vehicle_type, notes):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO dim_symbol_overrides (symbol, sector, asset_class, vehicle_type, notes, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (symbol) DO UPDATE SET
                    sector       = EXCLUDED.sector,
                    asset_class  = EXCLUDED.asset_class,
                    vehicle_type = EXCLUDED.vehicle_type,
                    notes        = EXCLUDED.notes,
                    updated_at   = NOW()
            """, (symbol, sector or None, asset_class or None, vehicle_type or None, notes or None))


def _delete_override(symbol):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dim_symbol_overrides WHERE symbol = %s", (symbol,))


def _get_exposure_tags():
    return pd.DataFrame(query("""
        SELECT t.symbol, array_agg(t.tag ORDER BY t.tag) AS tags
        FROM symbol_exposure_tags t
        GROUP BY t.symbol
        ORDER BY t.symbol
    """))


def _get_all_tags():
    rows = query("SELECT DISTINCT tag FROM symbol_exposure_tags ORDER BY tag")
    return [r["tag"] for r in rows]


def _set_exposure_tags(symbol, tags):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM symbol_exposure_tags WHERE symbol = %s", (symbol,))
            for tag in tags:
                cur.execute(
                    "INSERT INTO symbol_exposure_tags (symbol, tag) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (symbol, tag.strip()),
                )


def _add_sector(sector):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO dim_sectors (sector)
                VALUES (%s)
                ON CONFLICT (sector) DO NOTHING
            """, (sector,))


# ── load data ─────────────────────────────────────────────────────────────────

sectors = [r["sector"] for r in query("SELECT sector FROM dim_sectors ORDER BY sort_order, sector")]

symbols_df = pd.DataFrame(query("""
    SELECT
        p.symbol,
        COALESCE(ds.name, p.symbol)    AS name,
        o.sector                       AS override_sector,
        o.asset_class                  AS override_asset_class,
        o.vehicle_type                 AS override_vehicle_type,
        ds.sector                      AS yf_sector,
        ds.asset_class                 AS yf_asset_class,
        ds.vehicle_type                AS yf_vehicle_type,
        o.notes                        AS notes,
        COALESCE(o.sector,       ds.sector,      'Unknown')                           AS effective_sector,
        COALESCE(o.asset_class,  ds.asset_class, p.security_type, 'Unknown')          AS effective_asset_class,
        COALESCE(o.vehicle_type, ds.vehicle_type, p.security_type, 'Unknown')         AS effective_vehicle_type,
        CASE
            WHEN o.symbol IS NOT NULL THEN 'override'
            WHEN ds.sector IS NOT NULL THEN 'yfinance'
            ELSE 'unknown'
        END AS source
    FROM (SELECT DISTINCT symbol, security_type FROM positions
          WHERE (account_id_key, fetched_at) IN (
              SELECT account_id_key, MAX(fetched_at) FROM positions GROUP BY account_id_key
          )) p
    LEFT JOIN dim_symbols ds         ON ds.symbol = p.symbol
    LEFT JOIN dim_symbol_overrides o ON o.symbol = p.symbol
    ORDER BY source DESC, p.symbol
"""))


# ── tab layout ────────────────────────────────────────────────────────────────

tab_symbols, tab_exposure, tab_sectors = st.tabs(["Symbol Overrides", "Exposure Tags", "Manage Sectors"])


# ══ Symbol Overrides tab ══════════════════════════════════════════════════════
with tab_symbols:

    # summary badges
    n_unknown  = int((symbols_df["effective_sector"] == "Unknown").sum())
    n_override = int((symbols_df["source"] == "override").sum())
    n_yf       = int((symbols_df["source"] == "yfinance").sum())

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Symbols",   len(symbols_df))
    c2.metric("yfinance Resolved", n_yf)
    c3.metric("Unknown Sector",  n_unknown, delta=f"{n_override} overrides set",
              delta_color="off")

    st.divider()

    # filter
    filter_col, _ = st.columns([2, 5])
    show_filter = filter_col.selectbox(
        "Show", ["All", "Unknown sector only", "Overrides only"], index=0
    )

    display_df = symbols_df.copy()
    if show_filter == "Unknown sector only":
        display_df = display_df[display_df["effective_sector"] == "Unknown"]
    elif show_filter == "Overrides only":
        display_df = display_df[display_df["source"] == "override"]

    st.dataframe(
        display_df[[
            "symbol", "name",
            "effective_sector", "effective_asset_class", "effective_vehicle_type",
            "yf_sector", "override_sector", "source", "notes"
        ]].rename(columns={
            "symbol":                 "Symbol",
            "name":                   "Name",
            "effective_sector":       "Sector",
            "effective_asset_class":  "Asset Class",
            "effective_vehicle_type": "Vehicle Type",
            "yf_sector":              "yfinance Sector",
            "override_sector":        "Override Sector",
            "source":                 "Source",
            "notes":                  "Notes",
        }),
        use_container_width=True,
        hide_index=True,
    )

    st.divider()
    st.subheader("Set / Update Override")

    all_symbols   = sorted(symbols_df["symbol"].tolist())
    asset_classes = ["Equity", "Fixed Income", "Commodity", "Cash", "Alternatives", "Other"]
    vehicle_types = ["Stock", "ETF", "Mutual Fund", "Trust/CEF", "Bond/CD", "Other"]

    # Pre-fill from existing override when a symbol is selected
    def _current(col):
        row = symbols_df[symbols_df["symbol"] == all_symbols[0]]
        if not row.empty:
            return row.iloc[0].get(col)
        return None

    with st.form("override_form"):
        col_sym, col_sec, col_ac, col_vt = st.columns([2, 3, 2, 2])
        sel_symbol = col_sym.selectbox("Symbol", all_symbols)
        sel_sector = col_sec.selectbox("Sector", ["(clear override)"] + sectors)
        sel_ac     = col_ac.selectbox("Asset Class", ["(clear override)"] + asset_classes)
        sel_vt     = col_vt.selectbox("Vehicle Type", ["(clear override)"] + vehicle_types)
        notes_in   = st.text_input("Notes (optional)", placeholder="e.g. Copper futures ETF")

        submitted = st.form_submit_button("Save Override")

    if submitted:
        sector_val = None if sel_sector == "(clear override)" else sel_sector
        ac_val     = None if sel_ac     == "(clear override)" else sel_ac
        vt_val     = None if sel_vt     == "(clear override)" else sel_vt

        if sector_val is None and ac_val is None and vt_val is None:
            _delete_override(sel_symbol)
            msg = f"Override removed for **{sel_symbol}**."
        else:
            _upsert_override(sel_symbol, sector_val, ac_val, vt_val, notes_in)
            msg = (f"Override saved for **{sel_symbol}**: "
                   f"sector={sector_val or '—'}, asset_class={ac_val or '—'}, vehicle_type={vt_val or '—'}.")

        _refresh_mv()
        st.success(msg + " mv_allocations refreshed.")
        st.rerun()


# ══ Exposure Tags tab ═════════════════════════════════════════════════════════
with tab_exposure:

    st.caption(
        "Exposure tags are thematic labels (Uranium, Precious Metals, Copper, etc.) "
        "that are orthogonal to sector and asset class. A symbol can have multiple tags. "
        "Values in the Portfolio Overview theme chart overlap intentionally."
    )

    tags_df = _get_exposure_tags()
    all_tags = _get_all_tags()

    if not tags_df.empty:
        display_tags = tags_df.copy()
        display_tags["tags"] = display_tags["tags"].apply(lambda t: ", ".join(t) if t else "")
        st.dataframe(
            display_tags.rename(columns={"symbol": "Symbol", "tags": "Exposure Tags"}),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No exposure tags defined yet.")

    st.divider()
    st.subheader("Set Tags for Symbol")

    tag_symbols = sorted(symbols_df["symbol"].tolist())

    with st.form("exposure_tags_form"):
        sel_tag_symbol = st.selectbox("Symbol", tag_symbols, key="tag_sym")

        current_tags = []
        if not tags_df.empty:
            row = tags_df[tags_df["symbol"] == sel_tag_symbol]
            if not row.empty:
                current_tags = row.iloc[0]["tags"] or []

        sel_tags = st.multiselect(
            "Exposure Tags",
            options=sorted(set(all_tags + [
                "Uranium", "Precious Metals", "Gold", "Silver", "Platinum",
                "Broad Energy", "Copper", "Agriculture", "Volatility",
                "Rare Earths", "Technology", "Healthcare",
            ])),
            default=current_tags,
            help="Select all themes that apply. Values can overlap across symbols.",
        )

        new_tag = st.text_input("Add a new tag (if not in list above)", placeholder="e.g. Rare Earths")
        tag_submitted = st.form_submit_button("Save Tags")

    if tag_submitted:
        final_tags = list(sel_tags)
        if new_tag.strip():
            final_tags.append(new_tag.strip())
        _set_exposure_tags(sel_tag_symbol, final_tags)
        _refresh_mv()
        st.success(f"Tags saved for **{sel_tag_symbol}**: {', '.join(final_tags) or '(none)'}. mv_allocations refreshed.")
        st.rerun()


# ══ Manage Sectors tab ════════════════════════════════════════════════════════
with tab_sectors:

    st.subheader("Current Sectors")
    sectors_df = pd.DataFrame(query("SELECT sector, sort_order FROM dim_sectors ORDER BY sort_order, sector"))
    st.dataframe(sectors_df.rename(columns={"sector": "Sector", "sort_order": "Sort Order"}),
                 use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Add New Sector")

    with st.form("add_sector_form"):
        new_sector = st.text_input("Sector Name", placeholder="e.g. Precious Metals")
        add_submitted = st.form_submit_button("Add Sector")

    if add_submitted:
        if not new_sector.strip():
            st.error("Sector name cannot be blank.")
        elif new_sector.strip() in sectors:
            st.warning(f"**{new_sector.strip()}** already exists.")
        else:
            _add_sector(new_sector.strip())
            st.success(f"Added sector **{new_sector.strip()}**.")
            st.rerun()
