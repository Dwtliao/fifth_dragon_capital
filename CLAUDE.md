# Fifth Dragon Capital — Claude Context
_Last updated: June 20, 2026. Read this at the start of every session._

---

## Project Overview
Personal trading infrastructure. PostgreSQL DB + Python + Streamlit dashboard.
Owner: David Liao (david.liao@precisetarget.com)

**Active branch:** `feature/p29-strategy-performance`
**Push when done:** `git push --set-upstream origin feature/p29-strategy-performance`
**Git identity for commits:** always use `-c user.email="david.liao@precisetarget.com" -c user.name="David Liao"`

---

## Directory Structure

```
fifth_dragon_capital/
├── alerts/              # price alert poller + email notify
├── dashboard/
│   └── pages/
│       ├── P1_Pipeline_Status.py
│       ├── P2_Portfolio_Overview.py
│       ├── P3_Performance.py
│       ├── P4_Trading_History.py
│       ├── P5_Risk_Exposure.py
│       ├── P6_Physical_Metals.py
│       ├── P7_Market_Monitor.py
│       ├── P8_Commodities.py
│       ├── P9_Symbol_Admin.py
│       └── P10_Morning_Brief.py
├── etrade_sync/         # raw sync pipeline — DO NOT MODIFY
├── morning_brief/       # pre-market brief + journal sync
│   ├── brief.py
│   ├── fetchers.py
│   ├── formatter.py
│   ├── journal_sync.py
│   └── key_levels.yml   # legacy — key levels now in DB (key_levels table)
├── data_model/          # numbered SQL migration files (001_, 052_, etc.)
├── scripts/             # launchd plists + shell wrappers
└── logs/                # runtime logs
```

---

## Architecture

```
Layer 4: BI Layer (Streamlit)          ← Dashboards P1–P10
Layer 3: Intelligence Layer            ← Materialized views + derived tables
Layer 2: Analytical Schema             ← fact_* and dim_* tables
Layer 1: Trading Ledger                ← Normalized event log
Layer 0: Raw Sync                      ← E*TRADE API → Postgres raw tables
```

Raw tables are never modified by the BI layers.

---

## DB Connection

Via `etrade_sync.db.get_connection()`. Python: `/Users/davidliao/git_repos/py312/venv/bin/python`.

---

## Current State

All work through P10 Morning Brief is complete and merged to main. See `PLAN_BI.md` for full completed item list and known gaps.

**Next issue: #29 Pass 1** — `mv_strategy_performance` materialized view + P4 taxonomy dropdown + P/L by strategy, win rate, avg holding period charts + empty state.

---

## launchd Jobs

| Job | Schedule | Purpose |
|-----|----------|---------|
| `sync-daily` | 6:00am | Full E*TRADE sync + refresh materialized views |
| `morning-brief` | 6:45am | Generates `trading_diary/morning_brief.md` |
| `auth-reminder` | 10:00pm | Reminds to log in to E*TRADE before midnight token expiry |

---

## Key Design Decisions

- **Python:** always use `/Users/davidliao/git_repos/py312/venv/bin/python`
- **SQL migrations:** numbered `NNN_description.sql` in `data_model/`, run in sorted order by `db.py`
- **Materialized views:** must have `UNIQUE INDEX` for `REFRESH CONCURRENTLY`
- **Key levels:** stored in `key_levels` DB table (migration 066); YAML is legacy
- **Journal sync:** `journal_sync_log` table (migration 067) tracks processed files by mtime
- **Price alerts:** two creation paths exist (journal sync + P10 alert_above) — known design gap, no unique constraint yet

---

## Network Note

Sandbox bash tool cannot authenticate to GitHub over HTTPS. All `git push` commands must be run from the Mac terminal.
