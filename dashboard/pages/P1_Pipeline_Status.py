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

PYTHON = sys.executable
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
         "sync:transactions", "sync:orders", "build_ledger", "build_realized_pnl",
         "seed_symbols", "seed_dates", "seed_prices", "refresh_views", "migrate",
         "reconcile", "daily_sync", "weekly_sync"],
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
        ('transaction_ingest_audit', (SELECT count(*) FROM transaction_ingest_audit)),
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

# ── run jobs ──────────────────────────────────────────────────────────────────

st.subheader("Run Jobs")

JOBS = {
    "sync":               "Pull latest data from E*TRADE (accounts, balances, positions, transactions, orders)",
    "migrate":            "Apply SQL schema changes — drop + recreate all materialized views, re-run all SQL files",
    "refresh-views":      "Refresh all materialized views without re-syncing",
    "build-ledger":       "Rebuild the ledger table from transactions",
    "build-realized-pnl": "Recompute FIFO realized P/L from ledger",
    "seed-symbols":       "Fetch symbol metadata (sector, asset class) from yfinance",
    "seed-prices":        "Fetch SPY benchmark prices from yfinance",
    "seed-dates":         "Regenerate the dim_dates date spine",
    "reconcile":          "Compare ledger positions to latest API snapshot",
}

NEEDS_TOKEN = {"sync"}

col_job, col_btn = st.columns([3, 1])
with col_job:
    job = st.selectbox("Job", list(JOBS.keys()),
                       format_func=lambda j: f"{j}  —  {JOBS[j]}")
with col_btn:
    st.write("")
    st.write("")
    run_btn = st.button("▶ Run", type="primary", use_container_width=True)

# Per-job options
cmd_extra = []

if job == "sync":
    if not fresh:
        st.error("Token is expired — re-authenticate before running sync.")
        run_btn = False
    c1, c2 = st.columns(2)
    with c1:
        only = st.selectbox("Data type", ["all", "accounts", "balances", "positions", "transactions", "orders"])
        if only != "all":
            cmd_extra += ["--only", only]
    with c2:
        date_range = st.selectbox("Date range (transactions + orders)", ["default (90d)", "30 days", "365 days", "from beginning"])
        if date_range == "30 days":
            cmd_extra += ["--days", "30"]
        elif date_range == "365 days":
            cmd_extra += ["--days", "365"]
        elif date_range == "from beginning":
            cmd_extra += ["--from-beginning"]

elif job == "build-ledger":
    full_rebuild = st.checkbox("Full rebuild (truncate ledger before repopulating)")
    if full_rebuild:
        cmd_extra += ["--full-rebuild"]

if run_btn:
    cmd = [PYTHON, "-m", "etrade_sync", job] + cmd_extra
    output_box = st.empty()
    lines = []
    with st.spinner(f"Running {job}…"):
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

    st.session_state["last_job_output"] = "\n".join(lines)
    st.session_state["last_job_name"]   = job
    st.session_state["last_job_ok"]     = proc.returncode == 0
    st.rerun()

# Show persisted result from previous run
if "last_job_output" in st.session_state:
    ok = st.session_state["last_job_ok"]
    name = st.session_state["last_job_name"]
    if ok:
        st.success(f"{name} completed successfully.")
    else:
        st.error(f"{name} failed.")
    with st.expander("Output", expanded=not ok):
        st.code(st.session_state["last_job_output"], language=None)
