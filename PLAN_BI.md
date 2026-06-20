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

## Current State (as of 2026-06-20)

### ✅ Completed

| Layer | Item | Notes |
|---|---|---|
| 0 | Raw sync pipeline | accounts, balances, positions, transactions, orders, order_details |
| 0 | Scheduled sync (launchd) | Daily 6AM full sync, Sunday 7AM 30-day refresh, 10PM auth reminder |
| 0 | Pipeline monitoring | sync_log table, triggered_by tracking (manual vs launchd) |
| 0 | `migrate` command | Drop + recreate all materialized views + re-apply all SQL files. Run after any schema change. |
| 0 | CSV transaction backfill | `import-csv` CLI command loads E*TRADE CSV exports into transactions. Source-aware dedup handles API/CSV overlaps. 2025–2026 history loaded for all 4 accounts and committed to git for portability. |
| 0 | Transaction provenance layer (#36) | `transaction_identity.py` computes `source_payload_hash`, `canonical_fingerprint`, and `dedupe_signature` at ingest. `transaction_ingest_audit` table records every row's classification (canonical / cross_source_overlap / same_source_candidate). Atomic UPSERT on `(account_id_key, source_system, source_record_key)` — repeated syncs increment `observed_count` instead of appending rows. Legacy CSV:SHA1 IDs preserved for backward compatibility. `cleanup-audit` CLI command with `--dry-run` for maintenance. |
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
| 3 | `open_lots` | Remaining buy lots after FIFO matching, split-adjusted qty and price. Rebuilt alongside realized_gains. |
| 3 | `mv_portfolio_timeseries` | Daily portfolio value, returns, volatility, drawdown — all accounts aggregated |
| 3 | `mv_portfolio_timeseries_by_account` | Same as above with account_id_key as dimension — used by Performance page account filter |
| 3 | `mv_allocations` | Sector / asset class / vehicle_type breakdown with override priority: dim_symbol_overrides > yfinance. Includes `exposure_tags` array from symbol_exposure_tags. |
| 3 | `mv_attribution_timeseries` | Daily sector + asset class market value/P/L per account — attribution drill-downs |
| 3 | `market_prices` | SPY benchmark price history via yfinance |
| 3 | `mv_benchmark_comparison` | Portfolio return vs SPY, alpha, rolling comparison — all accounts |
| 3 | `mv_benchmark_comparison_by_account` | Per-account portfolio vs SPY — used when account filter is active |
| 4 | Pipeline Status page (P1) | Token alert, full job runner (all batch commands), live output, sync_log history |
| 4 | Portfolio Overview page (P2) | Account + position filters, KPIs, sector/asset class/vehicle-type donuts with labels + summary tables. Sector donut excludes Fixed Income/Cash (risk assets only). Theme exposure bar chart (horizontal, overlap note). Cash KPI uses `cash_available_for_invest` (correct for margin + IRA accounts). Position lot detail expander per multi-lot symbol with split-adjusted buy prices, cost basis, current value, P/L, days held, and quantity reconciliation warning. Themes column in positions table. |
| 4 | Performance page (P3) | Equity curve, drawdown, rolling returns, attribution by account/sector/asset class, realized P/L |
| 4 | Trading History page (P4) | P/L heatmap, cash flow/income charts, trade scatterplot, ledger explorer with active-filter display, strategy tag form |
| 4 | Symbol Admin page (P9) | Three tabs: Symbol Overrides (sector/asset class/vehicle_type per symbol, notes), Exposure Tags (many-to-many theme tags with multiselect + free-text), Manage Sectors. Saving auto-refreshes mv_allocations. |
| 2 | `vehicle_type` column | Added to `dim_symbols` and `dim_symbol_overrides`. Three independent classification axes: sector (equity theme), asset_class (Equity/Fixed Income/Commodity/Cash), vehicle_type (Stock/ETF/Mutual Fund/Trust/CEF/Bond/CD). Seeds in 061_add_vehicle_type.sql. |
| 2 | `symbol_exposure_tags` table | Many-to-many table: symbol → thematic tags (Uranium, Precious Metals, Gold, Silver, Copper, etc.). Tags are orthogonal to sector/asset_class and intentionally overlap across symbols. Schema in 052b, seeds in 062. |
| 0 | Cross-source dedup fix | Narrowed `dedupe_payload` in `transaction_identity.py` to exclude `settlement_date`, `description`, `description2`, and `fee` — all of which differ systematically between CSV and API representations of the same trade. 063_reset_dedupe_signatures.sql resets all signatures for recompute. |
| 4 | Lot provenance (#38) | Provenance column in Position Lot Detail showing source (api/csv) and classification per lot. `🔍` badge fires only for `cross_source_overlap` / `same_source_candidate` — API lots with no audit record show silently. Positions table columns are real numbers with column_config formatting (sortable, subtotals-ready). |
| 4 | P2 column swap fix | `% Portfolio` and `P/L %` columns were transposed in positions table rename — IUSXX showed 0% because pnl_pct (≈0 for money market) was labeled "% Portfolio". Fixed column order. |
| 4 | Risk & Exposure page (#28/#39) | P5 complete. See Page 5 spec below. |
| 4 | P4 Return % vs Trade Date | Second scatter added to Trades tab — sell_date on x-axis, stacked below existing Holding Days scatter. Full-width layout. |
| 4 | P2 Live E*TRADE quotes | `etrade_sync/market/quotes.py` wrapper. Manual "Fetch Live Quotes" button in sidebar — overrides snapshot market values with real-time E*TRADE prices for the session. Timestamp shown on banner. Graceful fallback to snapshot when token expired. |
| 4 | P2 P/L color coding | Unrealized P/L and P/L% columns green/bold for gains, red/bold for losses via pandas Styler. 2 decimal places on Unrealized P/L. |
| 4 | Physical Metals page (P6) | Fully separate from E*TRADE schema. `physical_holdings_pm` + `physical_prices_pm` tables (migration 064). Spot prices auto-fetched via yfinance (GC=F, SI=F, PL=F, PA=F) with manual override. Holdings table with live spot value and unrealized G/L. Add/delete holdings form. |
| 4 | Market Monitor page (P7) | Candlestick + volume charts for US indices (incl. DXY), global indices, ETFs, volatility/rates/bond futures, defensive sectors. Period selector: Intraday/5D/1M/3M/6M. Auto-refresh via `st.fragment(run_every=...)`. 2 charts per row. Price Alerts section at top: add/manage/delete/re-arm alerts, ▶ Run Alert Poll sidebar button. |
| 4 | Commodities page (P8) | Candlestick + volume charts with period selector for precious metals futures, energy futures, metals & miners (incl. PPLT, SBSW, SIL, SILJ, WPM, FNV, AEM, RGLD, GROY), uranium (SRUUF spot proxy + miners), copper, agriculture. Auto-refresh on intraday only. |
| 4 | Morning Brief page (P10) | Two tabs: 📋 Brief (renders trading_diary/morning_brief.md) + 🔑 Key Levels (edit stops/watch levels, saved to DB). Sync History tab shows per-journal extraction detail. ▶ Run Morning Pipeline = journal sync + brief in one click. |
| 0 | Price alerts system | `price_alerts` table (migration 065). `alerts/poller.py`: poll yfinance every N minutes, fire+re-arm logic. `alerts/notify.py`: email via smtplib. Managed via P7 UI. |
| 0 | Morning brief module | `morning_brief/brief.py`: generates daily pre-market markdown to `trading_diary/`. Fetches global indices, US futures, commodities, FX, vol via yfinance. Positions use E*TRADE real-time quotes (yfinance fallback). `morning_brief/fetchers.py`, `formatter.py`. launchd at 6:45am. |
| 0 | Key levels DB (migration 066) | `key_levels` table replaces `key_levels.yml`. Section (positions/watch), ticker, stop, support, resistance, alert_above, note. Seeds from YAML on first run. P10 reads/writes DB directly. |
| 0 | Journal sync (migration 067) | `morning_brief/journal_sync.py`: sends trading journals to Claude API, extracts stops/watch levels/conditional alerts, writes to `key_levels` + `price_alerts`. Tracks processed files in `journal_sync_log` by mtime. Integrated into P10 Morning Pipeline button. |

### Known Gaps / Open Issues

| Issue | Severity | Description |
|---|---|---|
| #35 | Medium | OAuth re-auth flow not yet in the dashboard UI — still requires terminal |
| #33 | Low | `dim_symbols.cusip` column never populated by `seed_symbols()` |
| #34 | Deferred | Ledger only sources from `transactions`; option/spread fills from `orders` not yet mapped |
| #41 | In progress | Market Intelligence Layer — Tier 1 alerts (price levels, volume anomaly, pre-market brief) done. Tier 2 (rotation signals, correlation break, FOMC countdown) and Tier 3 (Claude API snapshot) pending. |
| — | Design gap | `price_alerts` has two creation paths (journal sync + P10 Watch Levels alert_above) that can create duplicate alerts for same ticker. Options: (A) unique constraint on (ticker, condition), (B) remove alert_above→price_alerts sync from P10, alerts only via journal or P7 manual. |
| — | Design gap | E*TRADE OAuth tokens expire at midnight ET. Browser login invalidates API token. No automated re-auth possible without Selenium + 2FA interception (not worth the risk). Morning brief uses E*TRADE quotes when token valid, yfinance fallback silently. |

---

## Issue Queue and Build Order

Dependencies determine sequence. Do not start a page before its upstream data layer is correct.

### Remaining Issue Order

| Order | Issue | Depends on | Notes |
|---|---|---|---|
| 1 | **#29 Pass 1** | #27 | `mv_strategy_performance` + P4 charts + taxonomy dropdown + empty state |
| 3 | **#29 Pass 2** | Pass 1, definition of "capital deployed" | Capital deployed over time + P3 strategy breakout — deferred until Pass 1 proves useful |
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
| **#27** | ✅ Done | Trading History — P/L heatmap, cash flow/income charts, trade scatterplot, ledger explorer, strategy tag form |
| **#28** | ✅ Done | Risk & Exposure page (P5) — 5 sections: concentration, position sizing, loss watch, realized P/L, holding period |
| **#32** | ✅ Done | Split-adjusted FIFO cost basis |
| **#38** | ✅ Done | Lot provenance display — Provenance column, smart flagging, sortable positions table, override migration fix |
| **#42** | ✅ Done | Price level alerts — `price_alerts` table, `alerts/poller.py`, P7 management UI, email notify |
| **#43** | ✅ Done | Pre-market brief — `morning_brief/` module, launchd at 6:45am, P10 dashboard, journal→Claude→DB sync |
| **#28** | ✅ Done | Risk & Exposure page (P5) — see Page 5 spec |

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
- Three-column donut layout: Sector (risk assets only, excludes Fixed Income/Cash), Asset Class, Vehicle Type — all with inside-arc % labels
- Theme exposure horizontal bar chart — overlap note shown (a symbol can carry multiple tags)
- Positions table: real numeric columns with column_config formatting, click-to-sort on all headers, Vehicle Type and Themes columns
- Position lot detail expander: quantity reconciliation warning, Provenance column (api/csv/classification), `🔍` badge only for genuine duplicate candidates

### Page 3: Performance ✅ (done)
- Global filters: Account | Period (YTD/1Y/3Y/All) | SPY toggle
- KPIs: portfolio return, SPY return, alpha, max drawdown, volatility
- Equity curve vs SPY (switches data source on account filter)
- Drawdown from peak area chart
- Rolling 30-day return vs SPY
- Attribution tabs: By Account (overlaid equity curves), By Sector, By Asset Class (stacked area + P/L bar)
- Realized P/L by year with local year filter and lot detail expander

### Page 9: Symbol Admin ✅ (done)
- **Symbol Overrides tab**: table showing effective sector/asset class/vehicle_type with source (override/yfinance/unknown). Filter to "Unknown sector only". Override form: set sector, asset class, vehicle type, and notes per symbol. Saving auto-refreshes mv_allocations.
- **Exposure Tags tab**: table of symbol → tags. Form with multiselect (predefined tags + free-text new tag input). Tags saved and mv_allocations refreshed on submit.
- **Manage Sectors tab**: view canonical sector list with sort order, add new sectors.

### Page 4: Trading History ✅ (done, #29 Pass 1 pending)
- Win rate, avg win/loss, profit factor
- Realized P/L by ticker and holding period bucket (<1w, 1w–1m, 1m–1y, >1y)
- Monthly P/L heatmap (year × month grid)
- Trade scatterplot: return % vs holding days
- Searchable/filterable ledger explorer
- Strategy tag form: free-text tag input per realized gain (persists across FIFO rebuilds via relinking)
- **#29 Pass 1 pending:** `mv_strategy_performance` view + taxonomy dropdown + P/L by strategy, win rate, avg holding period charts + empty state

### Page 5: Risk & Exposure ✅ (done, #28/#39)
- **Snapshot strip (4 KPIs):** Largest Position, Top 3 Concentration, Cash Reserve, Commodity Tilt
- **Two-denominator framework:** portfolio exposure (total MV incl. Cash+FI) vs risk concentration (risk asset MV only, excl. Cash+FI). Sector chart, position sizing, and thematic callout all use risk-asset denominator. Asset class breakdown and % Portfolio column use portfolio denominator.
- **Section 1 — Concentration Risk:** sector bar chart (% of risk assets), asset class bar chart (% of portfolio), thematic callout tiles (% of risk assets)
- **Section 2 — Position Sizing:** risk assets only, sidebar-adjustable thresholds (default 🟡≥10% / 🔴≥20%), Recompute button, `% Risk Assets` column
- **Section 3 — Unrealized Loss Watch:** positions underwater, total loss KPI, count KPI, red bar chart, detail table. Cash excluded; Fixed Income included.
- **Section 4 — Realized P/L Summary:** all-time (no date filter). Total P/L, trade count, win rate KPIs. P/L by year bar chart (green/red). Per-symbol summary table.
- **Section 5 — Holding Period Risk:** open equity lots only (bonds excluded). Value-weighted avg holding days, % long-term KPIs. Bucket bar chart + summary table.
- **Long-term roadmap:** target dashboard in memory (project_p5_roadmap.md) — Risk Capacity gauge, diversification score, scatter plot. Deferred until P5 proves useful.

**Key design decisions:**
- `pct_of_portfolio` = portfolio exposure denominator (for allocation/net-worth views)
- `pct_of_risk_assets` = risk concentration denominator (for position sizing/sector concentration)
- Threshold inputs stored in `st.session_state.applied_thresholds` — only update on Recompute button press

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
| Cash KPI field? | Use `cash_available_for_invest` (`cashAvailableForInvestment` from E*TRADE API). `net_cash` includes margin buying power on margin accounts (wrong); `cash_balance` excludes money market funds on IRA accounts (wrong). `cash_available_for_invest` is correct for all account types. |
| E*TRADE transaction history depth? | API returns ~2 months for IRA Rollover despite requesting 2-year lookback. This is an E*TRADE API limitation, not a code bug. History available via CSV export only. |
| CSV backfill dedup strategy? | Two-pass source-aware dedup in `build_ledger()`. Pass A (cross-source): removes CSV rows shadowed by an API row using `ROUND(price, 2)` to handle CSV 2dp vs API precision. Pass B (same-source): removes API rows E*TRADE returned twice under different IDs using exact price, so genuine same-day fills at similar-but-distinct prices are never collapsed. |
| CSV files in git? | Yes — E*TRADE transaction CSVs contain no credentials and serve as portable transaction history. Committing them means a fresh clone can fully rebuild the pipeline without re-downloading from E*TRADE. Stored in `data/csv_exports/`. |
| Bond positions in lot detail? | Excluded from Portfolio Overview lot detail. Bonds use face-value quantities (e.g. 15,000 = $15,000 face) and price-per-$100-face, making qty × price misleading without a bond-specific formula. |
| Transaction dedup strategy? | Two-layer approach: (1) fingerprint layer in `transaction_identity.py` at ingest time — `dedupe_payload` contains only the stable business-key fields (account, event_type, symbol, transaction_date, quantity, price, amount at 2dp). Excludes settlement_date (CSV uses T+2 synthetic; API returns actual), description/description2 (always differ between sources), and fee (0.0 vs 0 vs absent). `dedupe_signature` = SHA-256 of this payload, so CSV and API representations of the same fill hash identically. (2) The `_CANONICAL_TRANSACTIONS_SQL` CTE in `build_ledger()` partitions by dedupe_signature and prefers API > CSV — a safety net for any row that slips through fingerprinting. |
| Audit trail growth? | `transaction_ingest_audit` uses upsert on `(account_id_key, source_system, source_record_key)` — one row per unique source record, `observed_count` increments on repeat syncs. `060_cleanup_transaction_ingest_audit.sql` collapses any historical duplicates and creates the unique index during schema bootstrap. `cleanup-audit --dry-run` available for inspection. |
| trade_tags schema (realized-gain vs generic)? | Keep the current `trade_tags(realized_gain_id → realized_gains.id)` model rather than the issue spec's generic `(source_table, source_id TEXT)`. The FK-enforced model is strongly typed and the relinking logic in `realized_pnl.py` already handles FIFO rebuilds correctly. The generic spec was written before `realized_gains` existed. |
| #29 Pass 2 timing? | Capital deployed over time and P3 strategy breakout are deferred until Pass 1 (mv_strategy_performance + P4 charts) proves useful and the definition of "capital deployed" is nailed down. |
