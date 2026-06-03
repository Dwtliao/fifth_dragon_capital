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
| 0 | `migrate` command | Drop + recreate all materialized views + re-apply all SQL files. Run after any schema change. |
| 1 | `ledger` table | Normalized event log, signed quantities, upsert-safe, source_line_id for future multi-leg |
| 2 | `dim_symbols` | yfinance metadata: sector, industry, asset class, exchange |
| 2 | `dim_sectors` | Canonical sector list with sort order — source of truth for sector dropdowns |
| 2 | `dim_symbol_overrides` | Manual sector/asset class overrides — wins over yfinance in all views. Managed via P9. |
| 2 | `dim_dates` | 2013–2027 date spine, NYSE trading day flag, ISO week/year |
| 2 | `dim_accounts` | SCD Type 2 history, account_sk surrogate, dim_accounts_current view |
| 2 | `fact_transactions` | One row per ledger event keyed to dims |
| 2 | `fact_positions` | Point-in-time position snapshots |
| 2 | `fact_cashflows` | Cash-only events: deposits, withdrawals, dividends, interest, fees |
| 2.5 | `reconciliation_log` | Ledger qty vs API positions snapshot, match/discrepancy/ledger_only/api_only |
| 3 | `mv_unrealized_pnl` | Materialized view from latest positions snapshot |
| 3 | `realized_gains` | Split-adjusted FIFO buy/sell lots, cost_basis, proceeds, realized_pnl, short/long term |
| 3 | `mv_portfolio_timeseries` | Daily portfolio value, returns, volatility, drawdown — all accounts aggregated |
| 3 | `mv_portfolio_timeseries_by_account` | Same as above with account_id_key as dimension — used by Performance page account filter |
| 3 | `mv_allocations` | Sector / asset class breakdown with override priority: dim_symbol_overrides > yfinance |
| 3 | `mv_attribution_timeseries` | Daily sector + asset class market value/P/L per account — attribution drill-downs |
| 3 | `market_prices` | SPY benchmark price history via yfinance |
| 3 | `mv_benchmark_comparison` | Portfolio return vs SPY, alpha, rolling comparison — all accounts |
| 3 | `mv_benchmark_comparison_by_account` | Per-account portfolio vs SPY — used when account filter is active |
| 4 | Pipeline Status page (P1) | Token alert, full job runner (all batch commands), live output, sync_log history |
| 4 | Portfolio Overview page (P2) | Account + position filters, KPIs, sector/asset class donuts with labels + summary tables |
| 4 | Performance page (P3) | Equity curve, drawdown, rolling returns, attribution by account/sector/asset class, realized P/L |
| 4 | Symbol Admin page (P9) | Sector/asset class override UI, manage sectors, auto-refreshes mv_allocations on save |

### Known Gaps / Open Issues

| Issue | Severity | Description |
|---|---|---|
| #35 | Medium | OAuth re-auth flow not yet in the dashboard UI — still requires terminal |
| #33 | Low | `dim_symbols.cusip` column never populated by `seed_symbols()` |
| #34 | Deferred | Ledger only sources from `transactions`; option/spread fills from `orders` not yet mapped |

---

## Issue Queue and Build Order

Dependencies determine sequence. Do not start a page before its upstream data layer is correct.

### Remaining Issue Order

| Order | Issue | Depends on | Notes |
|---|---|---|---|
| 1 | **#27** | ledger | Trading History — ledger explorer; strategy tag form still waits on #29 |
| 2 | **#28** | #23, `dim_symbols` | Risk & Exposure — sector concentration, position sizing, exposure analysis |
| 3 | **#29** | #27 | Strategy tags — depends on usable ledger explorer |
| 4 | **#35** | none | OAuth re-auth UI in Pipeline Status — two-step Streamlit widget |
| 5 | **#33** | none | Low-priority backfill for `dim_symbols.cusip` |
| 6 | **#34** | none | Deferred design pass for options / multi-leg order mapping |

### Completed

| Issue | Status | Notes |
|---|---|---|
| **#22** | ✅ Done | `mv_portfolio_timeseries` — all-account daily timeseries |
| **#23** | ✅ Done | `mv_allocations` with sector override priority |
| **#24** | ✅ Done | `market_prices` + `mv_benchmark_comparison` |
| **#25** | ✅ Done | Portfolio Overview — account filter, position filters, donut charts with labels |
| **#26** | ✅ Done | Performance — equity curve, drawdown, rolling returns, attribution, realized P/L |
| **#32** | ✅ Done | Split-adjusted FIFO cost basis |

---

## Dashboard Pages — Spec

### Page 1: Pipeline Status ✅ (done)
- Token freshness alert
- Job run history (sync_log) with filter by job name
- Table health (row counts)
- Run Jobs panel: all batch commands with live output streaming, persisted result after rerun

### Page 2: Portfolio Overview ✅ (done)
- Global account filter + position filters (symbol, sector, asset class)
- KPIs: total account value, invested MV, cash, unrealized P/L, P/L % — all respond to position filters
- Sector and asset class donut charts with % + $ slice labels and summary tables below
- Full positions table with P/L %

### Page 3: Performance ✅ (done)
- Global filters: Account | Period (YTD/1Y/3Y/All) | SPY toggle
- KPIs: portfolio return, SPY return, alpha, max drawdown, volatility
- Equity curve vs SPY (switches data source on account filter)
- Drawdown from peak area chart
- Rolling 30-day return vs SPY
- Attribution tabs: By Account (overlaid equity curves), By Sector, By Asset Class (stacked area + P/L bar)
- Realized P/L by year with local year filter and lot detail expander

### Page 9: Symbol Admin ✅ (done)
- Symbol table showing effective sector/asset class with source (override/yfinance/unknown)
- Filter to "Unknown sector only" to quickly find gaps
- Override form: set sector + asset class per symbol, with notes
- Saving auto-refreshes mv_allocations
- Manage Sectors tab: view canonical list, add custom sectors

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

### `mv_portfolio_timeseries`
Source: `positions` snapshots — all accounts aggregated

Key columns: `date`, `total_market_value`, `total_cost_basis`, `total_unrealized_pnl`, `daily_return_pct`, `rolling_7/30/90d_return_pct`, `rolling_volatility_30d`, `drawdown_from_peak_pct`

### `mv_portfolio_timeseries_by_account`
Source: same as above, partitioned by `account_id_key`

Same columns + `account_id_key`. Rolling and drawdown metrics are PARTITION BY account.

### `mv_allocations`
Source: latest positions snapshot + `dim_symbol_overrides` (priority) + `dim_symbols` (fallback)

Key columns: `account_id_key`, `symbol`, `sector`, `asset_class`, `market_value`, `cost_basis`, `unrealized_pnl`, `pct_of_portfolio`

### `mv_attribution_timeseries`
Source: daily positions snapshots joined to dim_symbol_overrides / dim_symbols

Key columns: `date`, `account_id_key`, `sector`, `asset_class`, `market_value`, `cost_basis`, `unrealized_pnl`, `pct_of_account`

One row per (date, account, sector, asset_class). Used for attribution stacked area charts.

### `mv_benchmark_comparison`
Source: `mv_portfolio_timeseries` + `market_prices` (SPY)

Key columns: `date`, `portfolio_daily_return_pct`, `spy_daily_return_pct`, `portfolio_cumulative_pct`, `spy_cumulative_pct`, `rolling_30d_portfolio_pct`, `rolling_30d_spy_pct`, `alpha_pct`

### `mv_benchmark_comparison_by_account`
Source: `mv_portfolio_timeseries_by_account` + `market_prices` (SPY)

Same columns + `account_id_key`. Cumulative returns anchored to each account's first date.

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
