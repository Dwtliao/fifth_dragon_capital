import datetime
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
from morning_brief.fetchers import fetch_positions_from_db, sync_positions_from_db

st.set_page_config(page_title="Morning Brief — Fifth Dragon Capital", layout="wide")
st.title("Morning Brief")

PROJECT_ROOT  = Path(__file__).parent.parent.parent
KEY_LEVELS_PATH = PROJECT_ROOT / "morning_brief" / "key_levels.yml"
DEFAULT_DIARY = Path.home() / "Library/CloudStorage/Dropbox/Etrade/trading_diary"

load_dotenv(PROJECT_ROOT / ".env")
diary      = Path(os.getenv("TRADING_DIARY", str(DEFAULT_DIARY)))
brief_path = diary / "morning_brief.md"


def _run_brief() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "morning_brief.brief"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    return result.stdout + result.stderr


def _load_key_levels() -> dict:
    if not KEY_LEVELS_PATH.exists():
        return {"positions": {}, "watch": {}}
    with open(KEY_LEVELS_PATH) as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("positions", {})
    data.setdefault("watch", {})
    return data


def _save_key_levels(data: dict):
    with open(KEY_LEVELS_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


# ── sidebar ────────────────────────────────────────────────────────────────────

st.sidebar.markdown("**Generate Brief**")
if st.sidebar.button("▶ Run Morning Brief", type="primary", use_container_width=True):
    with st.spinner("Fetching market data…"):
        output = _run_brief()
    st.sidebar.code(output.strip(), language=None)
    st.rerun()

st.sidebar.divider()
st.sidebar.markdown("**Positions**")
if st.sidebar.button("🔄 Sync Positions from DB", use_container_width=True,
                     help="Adds new holdings from E*TRADE DB; removes closed positions that have no stop/note set"):
    try:
        kl = _load_key_levels()
        updated, added, removed = sync_positions_from_db(kl)
        _save_key_levels(updated)
        msgs = []
        if added:
            msgs.append(f"Added: {', '.join(added)}")
        if removed:
            msgs.append(f"Removed: {', '.join(removed)}")
        if not added and not removed:
            msgs.append("Already in sync — no changes.")
        st.sidebar.success("\n".join(msgs))
        st.rerun()
    except RuntimeError as e:
        st.sidebar.error(str(e))
    except Exception as e:
        st.sidebar.error(f"Sync failed: {e}")

st.sidebar.divider()
if brief_path.exists():
    mtime = brief_path.stat().st_mtime
    st.sidebar.caption(
        f"Last generated:\n{datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')}"
    )

# ── tabs ───────────────────────────────────────────────────────────────────────

tab_brief, tab_levels = st.tabs(["📋 Brief", "🔑 Key Levels"])


# ══ Tab 1: Brief ══════════════════════════════════════════════════════════════

with tab_brief:
    if not brief_path.exists():
        st.info(
            f"No brief found at `{brief_path}`. "
            "Click **▶ Run Morning Brief** in the sidebar to generate one."
        )
    else:
        content = brief_path.read_text(encoding="utf-8")
        st.markdown(content)


# ══ Tab 2: Key Levels ═════════════════════════════════════════════════════════

with tab_levels:
    st.caption(
        "Changes here are written back to `morning_brief/key_levels.yml` immediately. "
        "Re-run the brief to see them reflected."
    )

    kl = _load_key_levels()

    # ── Positions ──────────────────────────────────────────────────────────────

    st.subheader("Positions")

    # Live DB snapshot (read-only context panel)
    db_rows = fetch_positions_from_db()
    if db_rows and "error" not in db_rows[0]:
        db_df = pd.DataFrame(db_rows)[["symbol", "quantity", "cost_basis", "market_value", "unrealized_pnl", "unrealized_pnl_pct"]]
        db_df.columns = ["Symbol", "Qty", "Cost Basis", "Mkt Value", "Unreal P/L $", "Unreal P/L %"]
        for col in ["Cost Basis", "Mkt Value", "Unreal P/L $"]:
            db_df[col] = db_df[col].apply(lambda x: f"{x:,.2f}" if x is not None else "—")
        db_df["Unreal P/L %"] = db_df["Unreal P/L %"].apply(lambda x: f"{x:+.2f}%" if x is not None else "—")
        st.caption("📊 Live from DB — read only. Use the table below to set stops/notes, then Save.")
        st.dataframe(db_df, use_container_width=True, hide_index=True)
    elif db_rows and "error" in db_rows[0]:
        st.warning(f"DB unavailable: {db_rows[0]['error']} — showing YAML positions only.")
    else:
        st.info("No positions in DB. Run a sync first or add tickers manually below.")

    st.caption("Edit stops and notes below. Click **💾 Save Key Levels** to persist.")

    pos_rows = [
        {"Ticker": t, "Stop": v.get("stop") or "", "Note": v.get("note") or ""}
        for t, v in (kl.get("positions") or {}).items()
    ]
    pos_df = pd.DataFrame(pos_rows, columns=["Ticker", "Stop", "Note"]) if pos_rows else \
             pd.DataFrame(columns=["Ticker", "Stop", "Note"])

    edited_pos = st.data_editor(
        pos_df,
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        column_config={
            "Ticker": st.column_config.TextColumn("Ticker", width="small"),
            "Stop":   st.column_config.NumberColumn("Stop", min_value=0.0, format="%.2f", width="small"),
            "Note":   st.column_config.TextColumn("Note", width="large"),
        },
        key="pos_editor",
    )

    st.divider()

    # ── Watch Levels ───────────────────────────────────────────────────────────

    st.subheader("Watch Levels")
    st.caption("Tickers shown in the **Key Levels** section with support/resistance distances.")

    watch_rows = [
        {
            "Ticker":      t,
            "Support":     v.get("support")     or "",
            "Resistance":  v.get("resistance")  or "",
            "Alert Above": v.get("alert_above") or "",
            "Note":        v.get("note")        or "",
        }
        for t, v in (kl.get("watch") or {}).items()
    ]
    watch_df = pd.DataFrame(watch_rows,
        columns=["Ticker", "Support", "Resistance", "Alert Above", "Note"]) if watch_rows else \
               pd.DataFrame(columns=["Ticker", "Support", "Resistance", "Alert Above", "Note"])

    edited_watch = st.data_editor(
        watch_df,
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        column_config={
            "Ticker":      st.column_config.TextColumn("Ticker",      width="small"),
            "Support":     st.column_config.NumberColumn("Support",     min_value=0.0, format="%.2f", width="small"),
            "Resistance":  st.column_config.NumberColumn("Resistance",  min_value=0.0, format="%.2f", width="small"),
            "Alert Above": st.column_config.NumberColumn("Alert Above", min_value=0.0, format="%.2f", width="small"),
            "Note":        st.column_config.TextColumn("Note",        width="large"),
        },
        key="watch_editor",
    )

    st.divider()

    if st.button("💾 Save Key Levels", type="primary"):
        # Rebuild positions dict from edited dataframe
        new_positions = {}
        for _, row in edited_pos.iterrows():
            ticker = str(row["Ticker"]).strip().upper()
            if not ticker:
                continue
            entry = {}
            if row["Stop"] not in ("", None):
                try:
                    entry["stop"] = float(row["Stop"])
                except (ValueError, TypeError):
                    pass
            if str(row["Note"]).strip():
                entry["note"] = str(row["Note"]).strip()
            new_positions[ticker] = entry

        # Rebuild watch dict from edited dataframe
        new_watch = {}
        for _, row in edited_watch.iterrows():
            ticker = str(row["Ticker"]).strip().upper()
            if not ticker:
                continue
            entry = {}
            for col, key in [("Support", "support"), ("Resistance", "resistance"), ("Alert Above", "alert_above")]:
                if row[col] not in ("", None):
                    try:
                        entry[key] = float(row[col])
                    except (ValueError, TypeError):
                        pass
            if str(row["Note"]).strip():
                entry["note"] = str(row["Note"]).strip()
            new_watch[ticker] = entry

        _save_key_levels({"positions": new_positions, "watch": new_watch})
        st.success("Key levels saved. Re-run the brief to reflect changes.")
        st.rerun()
