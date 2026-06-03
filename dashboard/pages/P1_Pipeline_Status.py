import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from dashboard.db import query, scalar

st.set_page_config(page_title="Pipeline Status — Fifth Dragon Capital", layout="wide")
st.title("Pipeline Status")

PYTHON = "/Users/davidliao/git_repos/py312/venv/bin/python"
TOKEN_FILE = Path.home() / ".config/etrade/tokens.json"
ET = ZoneInfo("America/New_York")


# ── helpers ───────────────────────────────────────────────────────────────────

def token_is_fresh():
    if not TOKEN_FILE.exists():
        return False, "missing"
    token_date = datetime.fromtimestamp(TOKEN_FILE.stat().st_mtime, tz=ET).date()
    today_et = datetime.now(tz=ET).date()
    return token_date == today_et, str(token_date)


def status_icon(status):
    return {"success": "✅", "failed": "❌", "token_stale": "⚠️", "running": "🔄"}.get(
        status or "", "—"
    )


# ── alerts ────────────────────────────────────────────────────────────────────

st.subheader("Alerts")

fresh, token_date = token_is_fresh()
if not fresh:
    label = "Token file not found." if token_date == "missing" else f"Token last written {token_date} ET."
    st.error(f"🔐 **E*TRADE token expired.** {label}  Run `python -m etrade_sync auth` then retry.")

last_sync = query("""
    SELECT status, started_at FROM sync_log
    WHERE job_name IN ('sync', 'daily_sync')
    ORDER BY started_at DESC LIMIT 1
""")
if last_sync and last_sync[0]["status"] in ("failed", "token_stale"):
    s = last_sync[0]
    st.warning(
        f"{status_icon(s['status'])} Last sync on "
        f"{s['started_at'].strftime('%Y-%m-%d %H:%M')} ET ended with **{s['status']}**."
    )

empty_tables = query("""
    SELECT tablename FROM (
        SELECT 'accounts'     AS tablename, count(*) n FROM accounts     UNION ALL
        SELECT 'transactions',               count(*)   FROM transactions UNION ALL
        SELECT 'positions',                  count(*)   FROM positions    UNION ALL
        SELECT 'ledger',                     count(*)   FROM ledger
    ) t WHERE n = 0
""")
for row in empty_tables:
    st.warning(f"⚠️ Table **{row['tablename']}** has 0 rows — sync may not have run.")

if fresh and not (last_sync and last_sync[0]["status"] in ("failed", "token_stale")) and not empty_tables:
    st.success("✅ All systems nominal.")

st.divider()

# ── job run history ───────────────────────────────────────────────────────────

st.subheader("Job Run History")

col_filter, col_limit = st.columns([2, 1])
with col_filter:
    job_filter = st.selectbox(
        "Filter by job",
        ["all", "sync", "sync:accounts", "sync:balances", "sync:positions",
         "sync:transactions", "sync:orders", "build_ledger", "seed_symbols",
         "seed_dates", "daily_sync", "weekly_sync"],
        label_visibility="collapsed",
    )
with col_limit:
    limit = st.selectbox("Show last", [20, 50, 100], label_visibility="collapsed")

where = "" if job_filter == "all" else "WHERE job_name = %(job)s"
runs = query(
    f"""
    SELECT job_name, started_at, status, duration_s, rows_synced, triggered_by
    FROM sync_log {where}
    ORDER BY started_at DESC LIMIT %(limit)s
    """,
    {"job": job_filter, "limit": limit},
)

if runs:
    df = pd.DataFrame(runs)
    df["icon"] = df["status"].map(status_icon)
    df["started_at"] = (
        pd.to_datetime(df["started_at"])
        .dt.tz_convert("America/New_York")
        .dt.strftime("%Y-%m-%d %H:%M:%S")
    )
    df["duration_s"] = df["duration_s"].apply(
        lambda x: f"{float(x):.1f}s" if x else "—"
    )
    df["rows_synced"] = df["rows_synced"].apply(
        lambda x: ", ".join(f"{k}: {v:,}" for k, v in x.items()) if x else "—"
    )
    st.dataframe(
        df[["icon", "started_at", "job_name", "status", "duration_s", "rows_synced", "triggered_by"]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "icon":         st.column_config.TextColumn(" ", width="small"),
            "started_at":   "Started (ET)",
            "job_name":     "Job",
            "status":       "Status",
            "duration_s":   "Duration",
            "rows_synced":  "Rows Synced",
            "triggered_by": "Source",
        },
    )
else:
    st.info("No runs recorded yet.")

st.divider()

# ── table row counts ──────────────────────────────────────────────────────────

st.subheader("Table Health")

counts = query("""
    SELECT * FROM (VALUES
        ('accounts',      (SELECT count(*) FROM accounts)),
        ('balances',      (SELECT count(*) FROM balances)),
        ('positions',     (SELECT count(*) FROM positions)),
        ('transactions',  (SELECT count(*) FROM transactions)),
        ('orders',        (SELECT count(*) FROM orders)),
        ('order_details', (SELECT count(*) FROM order_details)),
        ('ledger',        (SELECT count(*) FROM ledger)),
        ('dim_symbols',   (SELECT count(*) FROM dim_symbols)),
        ('dim_dates',     (SELECT count(*) FROM dim_dates)),
        ('sync_log',      (SELECT count(*) FROM sync_log))
    ) AS t(tablename, row_count)
""")

cols = st.columns(5)
for i, row in enumerate(counts):
    with cols[i % 5]:
        st.metric(label=row["tablename"], value=f"{row['row_count']:,}")

st.divider()

# ── last sync timestamps ──────────────────────────────────────────────────────

st.subheader("Last Sync Times")

sync_times = query("""
    SELECT account_id_key, data_type, last_synced_at
    FROM sync_state
    ORDER BY data_type, account_id_key
""")

if sync_times:
    df_st = pd.DataFrame(sync_times)
    df_st["last_synced_at"] = (
        pd.to_datetime(df_st["last_synced_at"])
        .dt.tz_convert("America/New_York")
        .dt.strftime("%Y-%m-%d %H:%M")
    )
    st.dataframe(
        df_st,
        use_container_width=True,
        hide_index=True,
        column_config={"last_synced_at": "Last Synced (ET)"},
    )
else:
    st.info("No sync_state entries yet.")

st.divider()

# ── run sync now ──────────────────────────────────────────────────────────────

st.subheader("Run Sync Now")

if not fresh:
    st.error("Token is expired — re-authenticate before running sync.")
else:
    col_only, col_btn = st.columns([2, 1])
    with col_only:
        only = st.selectbox(
            "Data type",
            ["all", "accounts", "balances", "positions", "transactions", "orders"],
        )
    with col_btn:
        st.write("")
        st.write("")
        run_btn = st.button("▶ Run Sync", type="primary", use_container_width=True)

    if run_btn:
        cmd = [PYTHON, "-m", "etrade_sync", "sync"]
        if only != "all":
            cmd += ["--only", only]

        output_box = st.empty()
        lines = []
        with st.spinner("Syncing…"):
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(Path(__file__).parent.parent.parent),
            )
            for line in proc.stdout:
                lines.append(line.rstrip())
                output_box.code("\n".join(lines), language=None)
            proc.wait()

        if proc.returncode == 0:
            st.success("Sync completed successfully.")
        else:
            st.error("Sync failed — see output above.")

        st.rerun()
