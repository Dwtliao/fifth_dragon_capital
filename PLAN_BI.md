# Fifth Dragon Capital — BI & Analytics Plan

## Architecture Overview

```
┌─────────────────────────────────────────┐
│  Layer 4: BI Layer (Streamlit)          │  ← Dashboards, charts, exploration
├─────────────────────────────────────────┤
│  Layer 3: Intelligence Layer            │  ← P/L, metrics, returns, allocations
│  (Materialized views / derived tables)  │
├─────────────────────────────────────────┤
│  Layer 2: Analytical Schema             │  ← Star schema: facts + dimensions
│  (fact_* and dim_* tables)              │
├─────────────────────────────────────────┤
│  Layer 1: Trading Ledger                │  ← Normalized event log (foundation)
├─────────────────────────────────────────┤
│  Layer 0: Raw Sync                      │  ← E*TRADE API → Postgres raw tables
│  accounts, balances, positions,         │
│  transactions, orders, order_details    │
└─────────────────────────────────────────┘
```

Raw tables are never modified by the BI layers — they stay as the source of truth.

---

## Data Model Design Principles

These apply to all SQL files in `data_model/` and Python analytics modules.

**Rounding policy**
- Dollar/monetary columns (`market_value`, `cost_basis`, `proceeds`, etc.): `ROUND(…::numeric, 2)` — two decimal places is semantically meaningful for currency.
- Rate/percentage/metric columns (`daily_return_pct`, `pct_of_portfolio`, `rolling_volatility`, `drawdown`, etc.): store as **double precision, no rounding** — the view is the source of truth; the dashboard and query layer handle display formatting. Rounding rates in the view would silently discard precision needed for charts and compounding calculations.

**Numbered SQL files**
- All schema files are prefixed with a three-digit number (`001_`, `052_`, etc.) so `db.py` can execute them in sorted order and guarantee FK-safe creation. This is the same pattern used by Flyway and Django migrations.

**Materialized views vs tables**
- Use a **materialized view** when the result is derived purely from existing tables and can be fully recomputed by a SQL refresh (e.g. `mv_unrealized_pnl`, `mv_allocations`).
- Use a **computed table** (truncate + rebuild from Python) when the logic requires stateful iteration that SQL can't express cleanly (e.g. `realized_gains` — FIFO matching with split adjustments).
- All materialized views must have a `UNIQUE INDEX` so they support `REFRESH CONCURRENTLY` (required by psycopg2 autocommit mode).

**Raw tables are immutable from the BI layer**
- Layers 1–4 only read from Layer 0. Raw sync tables (`positions`, `transactions`, etc.) are never updated or deleted by analytics code.

---

## Current State (as of 2026-06-03)

### ✅ Completed

| Layer | Item | Notes |
|---|---|---|
| 0 | Raw sync pipeline | accounts, balances, positions, transactions, orders, order_details |
| 0 | Scheduled sync (launchd) | Daily 6AM full sync, Sunday 7AM 30-day refresh, 10PM auth reminder |
| 0 | Pipeline monitoring | sync_log table, triggered_by tracking (manual vs launchd) |
| 1 | `ledger` table | Normalized event log, signed quantities, upsert-safe, source_line_id for future multi-leg |
| 2 | `dim_symbols` | yfinance metadata: sector, industry, asset class, exchange |
| 2 | `dim_dates` | 2013–2027 date spine, NYSE trading day flag, ISO week/year |
| 2 | `dim_accounts` | SCD Type 2 history, account_sk surrogate, dim_accounts_current view |
| 2 | `fact_transactions` | One row per ledger event keyed to dims |
| 2 | `fact_positions` | Point-in-time position snapshots |
| 2 | `fact_cashflows` | Cash-only events: deposits, withdrawals, dividends, interest, fees |
| 2.5 | `reconciliation_log` | Ledger qty vs API positions snapshot, match/discrepancy/ledger_only/api_only |
| 3 | `mv_unrealized_pnl` | Materialized view from latest positions snapshot |
| 3 | `realized_gains` | Split-adjusted FIFO buy/sell lots, cost_basis, proceeds, realized_pnl, short/long term |
| 3 | `mv_portfolio_timeseries` | Daily portfolio value, returns, volatility, drawdown |
| 3 | `mv_allocations` | Sector / asset class breakdown and portfolio weights |
| 3 | `market_prices` | SPY benchmark price history via yfinance |
| 3 | `mv_benchmark_comparison` | Portfolio return vs SPY, alpha, rolling comparison |
| 4 | Pipeline Status page | Token freshness, job history, table health, Run Sync Now with live output |

### Known Gaps / Open Issues

| Issue | Severity | Description |
|---|---|---|
| #33 | Low | `dim_symbols.cusip` column never populated by `seed_symbols()` |
| #34 | Deferred | Ledger only sources from `transactions`; option/spread fills from `orders` not yet mapped |

---

## Issue Queue and Build Order

Dependencies determine sequence. Do not start a page before its upstream data layer is correct.

### Remaining Issue Order

| Order | Issue | Depends on | Notes |
|---|---|---|---|
| 1 | **#25** | #23, `mv_unrealized_pnl` | Portfolio Overview is now unblocked and can be built independently of the benchmark work. |
| 2 | **#26** | #22, #24 | Performance needs both the portfolio timeseries and benchmark view. Realized P/L is now accurate because #32 is done. |
| 3 | **#27** | ledger | Trading History can start now as a ledger explorer; the tag form still waits on #29. |
| 4 | **#28** | #23, `dim_symbols` | Risk & Exposure is now unblocked by allocations and symbol metadata. |
| 5 | **#29** | #27 | Strategy tags depend on a usable ledger explorer. |
| 6 | **#33** | none | Low-priority backfill for fixed-income metadata. |
| 7 | **#34** | none | Deferred design pass for options / multi-leg order mapping. |

### Already completed, but worth remembering

| Issue | Status | Why it matters |
|---|---|---|
| **#22** | Done | Provides `mv_portfolio_timeseries` for performance charts and benchmark comparison. |
| **#23** | Done | Provides `mv_allocations` for Portfolio Overview and Risk & Exposure. |
| **#24** | Done | Provides `market_prices` and `mv_benchmark_comparison` for benchmark charts. |
| **#32** | Done | Split-adjusted FIFO is now in place, so realized P/L is correct for split positions. |

---

## Dashboard Pages — Spec

### Page 1: Pipeline Status ✅ (done)
- Token freshness alert
- Last sync status and job run history (sync_log)
- Table health (row counts)
- Run Sync Now button with live output streaming

### Page 2: Portfolio Overview (#25)
- Total account value, cash, invested capital
- Unrealized P/L vs cost basis
- Allocation pie: sector / asset class (from mv_allocations)
- Top positions by market value
- Exposure by ticker

### Page 3: Performance (#26)
- Equity curve (cumulative return from mv_portfolio_timeseries)
- Daily/weekly/monthly return table
- Rolling 30/90-day return vs SPY benchmark (mv_benchmark_comparison)
- Rolling 30-day volatility
- Max drawdown / drawdown-from-peak chart
- Realized P/L by year/quarter (from realized_gains)

### Page 4: Trading History (#27)
- Win rate, avg win/loss, profit factor
- Realized P/L by ticker and holding period bucket (<1w, 1w–1m, 1m–1y, >1y)
- Monthly P/L heatmap (year × month grid)
- Trade scatterplot: return % vs holding days
- Searchable/filterable ledger explorer
- Strategy tag form (#29)

### Page 5: Risk & Exposure (#28)
- Sector concentration bar chart (from mv_allocations)
- Asset class breakdown
- Largest losing positions / trades
- Short-term vs long-term holding breakdown
- Position sizing as % of portfolio

---

## Intelligence Layer — View Specs

### `mv_portfolio_timeseries` (#22)
Source: `positions` snapshots (one per `fetched_at` timestamp)

Key columns:
- `date`, `total_market_value`, `total_cost_basis`, `total_unrealized_pnl`
- `daily_return_pct` (day-over-day market value change)
- `rolling_7d_return_pct`, `rolling_30d_return_pct`, `rolling_90d_return_pct`
- `rolling_volatility_30d` (stddev of daily returns)
- `drawdown_from_peak_pct` (peak-to-trough from running max)

### `mv_allocations` (#23)
Source: latest positions snapshot + `dim_symbols`

Key columns:
- `symbol`, `sector`, `asset_class`, `market_value`, `pct_of_portfolio`
- Aggregated: sector totals, asset class totals

### `mv_benchmark_comparison` (#24)
Source: `mv_portfolio_timeseries` + `market_prices` (SPY daily close via yfinance)

Key columns:
- `date`, `portfolio_daily_return_pct`, `spy_daily_return_pct`, `alpha_pct`
- `portfolio_cumulative_pct`, `spy_cumulative_pct`
- `rolling_30d_portfolio_pct`, `rolling_30d_spy_pct`

Requires a `market_prices` table seeded from yfinance.

---

## Resolved Design Decisions

| Question | Decision |
|---|---|
| FIFO vs average cost? | FIFO — US tax standard |
| Source for symbol metadata? | yfinance (free, already used) |
| Source for benchmark prices? | yfinance (SPY daily close) |
| Materialized view refresh frequency? | After every full sync (auto-wired in `__main__.py`) |
| Strategy tags: manual or auto? | Manual via Streamlit form first; auto-tag options later (#34 dependency) |
| Dashboard deployment? | Local only for now; Streamlit Cloud possible later |
| Orders → ledger? | Deferred (#34) — stock fills covered by transactions; option/spread mapping needs separate design pass |
