import datetime
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
from morning_brief.fetchers import (
    fetch_positions_from_db, sync_positions_from_db,
    load_key_levels_from_db, save_key_levels_to_db,
)

st.set_page_config(page_title="Morning Brief — Fifth Dragon Capital", layout="wide")
st.title("Morning Brief")

PROJECT_ROOT  = Path(__file__).parent.parent.parent
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


# ── sidebar ────────────────────────────────────────────────────────────────────

if brief_path.exists():
    mtime = brief_path.stat().st_mtime
    st.sidebar.caption(f"Brief: {datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')}")

# ── primary: one-click morning pipeline ───────────────────────────────────────
if st.sidebar.button("▶ Run Morning Pipeline", type="primary", use_container_width=True,
                     help="1) Sync latest journal (if updated)  2) Generate morning brief"):
    diary   = Path(os.getenv("TRADING_DIARY", str(DEFAULT_DIARY)))
    journals = sorted(diary.glob("trading_journal_*.md"))
    output_lines = []

    # Step 1: sync latest journal if it exists
    if journals:
        latest = journals[-1]
        with st.spinner(f"Step 1/2 — syncing {latest.name}…"):
            r1 = subprocess.run(
                [sys.executable, "-m", "morning_brief.journal_sync", "--file", str(latest)],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT),
            )
        output_lines.append(r1.stdout.strip())

    # Step 2: generate brief
    with st.spinner("Step 2/2 — generating brief…"):
        r2 = subprocess.run(
            [sys.executable, "-m", "morning_brief.brief"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
    output_lines.append(r2.stderr.strip())

    st.sidebar.code("\n".join(filter(None, output_lines)), language=None)
    st.rerun()

st.sidebar.divider()

# ── manual controls ────────────────────────────────────────────────────────────
if st.sidebar.button("▶ Brief only", use_container_width=True,
                     help="Regenerate brief without re-syncing journal"):
    with st.spinner("Fetching market data…"):
        output = _run_brief()
    st.sidebar.code(output.strip(), language=None)
    st.rerun()

if st.sidebar.button("🔄 Sync Latest Journal", use_container_width=True,
                     help="Extract stops/levels/alerts from latest journal via Claude API"):
    diary = Path(os.getenv("TRADING_DIARY", str(DEFAULT_DIARY)))
    journals = sorted(diary.glob("trading_journal_*.md"))
    if not journals:
        st.sidebar.error("No journal files found.")
    else:
        latest = journals[-1]
        with st.sidebar:
            with st.spinner(f"Reading {latest.name}…"):
                result = subprocess.run(
                    [sys.executable, "-m", "morning_brief.journal_sync",
                     "--file", str(latest)],
                    capture_output=True, text=True, cwd=str(PROJECT_ROOT),
                )
        output = result.stdout + (result.stderr if result.returncode != 0 else "")
        st.sidebar.code(output.strip(), language=None)
        st.rerun()

if st.sidebar.button("🔄 Sync Positions from DB", use_container_width=True,
                     help="Add new E*TRADE holdings, remove closed ones"):
    try:
        kl = load_key_levels_from_db()
        updated, added, removed = sync_positions_from_db(kl)
        save_key_levels_to_db(updated)
        msgs = []
        if added:   msgs.append(f"Added: {', '.join(added)}")
        if removed: msgs.append(f"Removed: {', '.join(removed)}")
        if not msgs: msgs.append("Already in sync.")
        st.sidebar.success("\n".join(msgs))
        st.rerun()
    except RuntimeError as e:
        st.sidebar.error(str(e))
    except Exception as e:
        st.sidebar.error(f"Sync failed: {e}")

if st.sidebar.button("🔄 Sync All Journals", use_container_width=True,
                     help="Process all unsynced journals"):
    with st.sidebar:
        with st.spinner("Syncing all journals…"):
            result = subprocess.run(
                [sys.executable, "-m", "morning_brief.journal_sync"],
                capture_output=True, text=True, cwd=str(PROJECT_ROOT),
            )
    st.sidebar.code((result.stdout + result.stderr).strip(), language=None)
    st.rerun()

# ── tabs ───────────────────────────────────────────────────────────────────────

tab_brief, tab_levels, tab_history = st.tabs(["📋 Brief", "🔑 Key Levels", "📜 Sync History"])


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
        "Edit stops, levels, and notes below. "
        "Click **💾 Save** on any row to persist, or use **💾 Save All** at the bottom."
    )

    kl = load_key_levels_from_db()

    # ── Positions ──────────────────────────────────────────────────────────────

    st.subheader("Positions")

    db_rows = fetch_positions_from_db()
    if db_rows and "error" not in db_rows[0]:
        db_df = pd.DataFrame(db_rows)[["symbol","quantity","cost_basis","market_value","unrealized_pnl","unrealized_pnl_pct"]]
        db_df.columns = ["Symbol","Qty","Cost Basis","Mkt Value","Unreal P/L $","Unreal P/L %"]
        for col in ["Cost Basis","Mkt Value","Unreal P/L $"]:
            db_df[col] = db_df[col].apply(lambda x: f"${x:,.2f}" if x is not None else "—")
        db_df["Unreal P/L %"] = db_df["Unreal P/L %"].apply(lambda x: f"{x:+.2f}%" if x is not None else "—")
        st.caption("📊 Last E*TRADE sync snapshot (read-only) — brief uses real-time E*TRADE quotes when token is valid")
        st.dataframe(db_df, use_container_width=True, hide_index=True)
    elif db_rows and "error" in db_rows[0]:
        st.warning(f"DB unavailable: {db_rows[0]['error']}")

    st.caption("Set stops and notes per position:")

    pos_dict = dict(kl.get("positions") or {})
    new_positions = {}

    for ticker, vals in pos_dict.items():
        c1, c2, c3, c4 = st.columns([1, 1, 3, 1])
        c1.markdown(f"**{ticker}**")
        stop = c2.number_input(
            "Stop", value=float(vals.get("stop") or 0.0),
            min_value=0.0, step=0.01, format="%.2f",
            key=f"pos_stop_{ticker}", label_visibility="collapsed"
        )
        note = c3.text_input(
            "Note", value=vals.get("note") or "",
            key=f"pos_note_{ticker}", label_visibility="collapsed",
            placeholder="note…"
        )
        delete = c4.checkbox("🗑", key=f"pos_del_{ticker}", help="Mark for deletion")
        if not delete:
            entry = {}
            if stop > 0:
                entry["stop"] = stop
            if note.strip():
                entry["note"] = note.strip()
            new_positions[ticker] = entry

    st.divider()

    # Add new position
    with st.expander("➕ Add Position"):
        with st.form("add_position_form"):
            ap1, ap2, ap3 = st.columns([1, 1, 3])
            new_ticker = ap1.text_input("Ticker").strip().upper()
            new_stop   = ap2.number_input("Stop", min_value=0.0, step=0.01, format="%.2f")
            new_note   = ap3.text_input("Note", placeholder="optional note…")
            if st.form_submit_button("Add", type="primary"):
                if new_ticker:
                    entry = {}
                    if new_stop > 0:
                        entry["stop"] = new_stop
                    if new_note.strip():
                        entry["note"] = new_note.strip()
                    new_positions[new_ticker] = entry
                    kl["positions"] = new_positions
                    save_key_levels_to_db(kl)
                    st.success(f"Added {new_ticker}.")
                    st.rerun()

    st.divider()

    # ── Watch Levels ───────────────────────────────────────────────────────────

    st.subheader("Watch Levels")
    st.caption("Support, resistance, and alert levels shown in the Key Levels section of the brief.")

    watch_dict = dict(kl.get("watch") or {})
    new_watch = {}

    for ticker, vals in watch_dict.items():
        st.markdown(f"**{ticker}**")
        w1, w2, w3, w4, w5 = st.columns([1, 1, 1, 3, 1])
        support    = w1.number_input("Support",    value=float(vals.get("support")     or 0.0), min_value=0.0, step=0.01, format="%.2f", key=f"w_sup_{ticker}",  label_visibility="collapsed")
        resistance = w2.number_input("Resistance", value=float(vals.get("resistance")  or 0.0), min_value=0.0, step=0.01, format="%.2f", key=f"w_res_{ticker}",  label_visibility="collapsed")
        alert_abv  = w3.number_input("Alert",      value=float(vals.get("alert_above") or 0.0), min_value=0.0, step=0.01, format="%.2f", key=f"w_alrt_{ticker}", label_visibility="collapsed")
        note       = w4.text_input("Note", value=vals.get("note") or "", key=f"w_note_{ticker}", label_visibility="collapsed", placeholder="note…")
        delete     = w5.checkbox("🗑", key=f"w_del_{ticker}", help="Mark for deletion")
        w1.caption("support"); w2.caption("resistance"); w3.caption("alert above")

        if not delete:
            entry = {}
            if support    > 0: entry["support"]     = support
            if resistance > 0: entry["resistance"]  = resistance
            if alert_abv  > 0: entry["alert_above"] = alert_abv
            if note.strip():   entry["note"]        = note.strip()
            new_watch[ticker] = entry

    st.divider()

    # Add new watch level
    with st.expander("➕ Add Watch Level"):
        with st.form("add_watch_form"):
            aw1, aw2, aw3, aw4, aw5 = st.columns([1, 1, 1, 1, 3])
            wt = aw1.text_input("Ticker").strip().upper()
            ws = aw2.number_input("Support",    min_value=0.0, step=0.01, format="%.2f")
            wr = aw3.number_input("Resistance", min_value=0.0, step=0.01, format="%.2f")
            wa = aw4.number_input("Alert Above", min_value=0.0, step=0.01, format="%.2f")
            wn = aw5.text_input("Note")
            if st.form_submit_button("Add", type="primary"):
                if wt:
                    entry = {}
                    if ws > 0: entry["support"]     = ws
                    if wr > 0: entry["resistance"]  = wr
                    if wa > 0: entry["alert_above"] = wa
                    if wn.strip(): entry["note"]    = wn.strip()
                    new_watch[wt] = entry
                    kl["watch"] = new_watch
                    save_key_levels_to_db(kl)
                    st.success(f"Added {wt}.")
                    st.rerun()

    st.divider()

    if st.button("💾 Save All", type="primary"):
        save_key_levels_to_db({"positions": new_positions, "watch": new_watch})

        # Auto-sync alert_above → price_alerts; delete old then insert so threshold stays current
        from dashboard.db import execute as db_execute
        synced = []
        for ticker, vals in new_watch.items():
            alert_above = vals.get("alert_above")
            if not alert_above or alert_above <= 0:
                continue
            db_execute(
                "DELETE FROM price_alerts WHERE ticker = %s AND condition = 'above' AND label LIKE 'Watch: %%'",
                (ticker,)
            )
            db_execute(
                "INSERT INTO price_alerts (ticker, label, condition, threshold) VALUES (%s, %s, 'above', %s)",
                (ticker, f"Watch: {ticker} above {alert_above}", alert_above)
            )
            synced.append(f"{ticker} > {alert_above}")

        msg = "Key levels saved."
        if synced:
            msg += f"  Also pushed to Price Alerts: {', '.join(synced)}"
        st.success(msg)
        st.rerun()


# ══ Tab 3: Sync History ═══════════════════════════════════════════════════════

with tab_history:
    from dashboard.db import query as db_query

    st.caption("One row per journal file processed. Re-syncing a file overwrites its row.")

    log = db_query("""
        SELECT file_path, file_mtime, synced_at, positions_updated, watch_updated,
               alerts_created, dry_run, extracted_json
        FROM journal_sync_log
        ORDER BY synced_at DESC
    """)

    if not log:
        st.info("No journals synced yet. Use **🔄 Sync Latest Journal** in the sidebar.")
    else:
        for r in log:
            name         = Path(r["file_path"]).name
            synced_at    = r["synced_at"].strftime("%Y-%m-%d %H:%M")
            journal_mtime = datetime.datetime.fromtimestamp(r["file_mtime"]).strftime("%Y-%m-%d %H:%M")
            summary      = (f"{r['positions_updated']} positions  •  "
                            f"{r['watch_updated']} watch levels  •  "
                            f"{r['alerts_created']} alerts")
            dry_tag      = "  _(dry run)_" if r["dry_run"] else ""
            label        = f"**{name}** — journal: {journal_mtime}  •  synced: {synced_at}  •  {summary}{dry_tag}"

            with st.expander(label):
                ex = r.get("extracted_json") or {}
                if not ex:
                    st.caption("No extraction detail stored.")
                else:
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        st.markdown("**Positions**")
                        for p in ex.get("positions") or []:
                            stop_s = f"  stop {p['stop']}" if p.get("stop") else ""
                            st.markdown(f"- `{p['ticker']}`{stop_s}  _{p.get('note','')}_")
                    with c2:
                        st.markdown("**Watch Levels**")
                        for w in ex.get("watch_levels") or []:
                            parts = []
                            if w.get("support"):    parts.append(f"sup {w['support']}")
                            if w.get("resistance"): parts.append(f"res {w['resistance']}")
                            st.markdown(f"- `{w['ticker']}`  {', '.join(parts)}")
                    with c3:
                        st.markdown("**Alerts Created**")
                        for a in ex.get("price_alerts") or []:
                            st.markdown(f"- `{a['ticker']}` {a['condition']} {a['threshold']}  _{a.get('label','')}_")
