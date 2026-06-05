# Fifth Dragon Capital — E*TRADE Data Pipeline

A Python CLI that syncs E*TRADE account data to a local Postgres database and builds a personal finance analytics layer on top of it. Pulls accounts, balances, positions, transactions, and orders; constructs a normalized ledger; runs FIFO cost basis matching; and exposes everything through a Streamlit dashboard.

---

## Prerequisites

- Python 3.12+
- PostgreSQL running locally
- E*TRADE API credentials ([developer.etrade.com](https://developer.etrade.com))
- `terminal-notifier` for macOS background notifications: `brew install terminal-notifier`

---

## Setup

```bash
# 1. Clone and enter the project
git clone https://github.com/Dwtliao/fifth_dragon_capital.git
cd fifth_dragon_capital

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and fill in your credentials:
#   ETRADE_CONSUMER_KEY=...
#   ETRADE_CONSUMER_SECRET=...
#   ETRADE_DEV=true          # false for production
#   DATABASE_URL=postgresql://postgres@localhost:5432/yourdb
```

---

## First Run

```bash
# Step 1: Authenticate with E*TRADE (opens browser)
python -m etrade_sync auth
```

This opens your browser to the E*TRADE authorization page. After approving, paste the verifier code into the terminal. Your OAuth tokens are saved to `~/.config/etrade/tokens.json` (mode 600).

Tokens expire daily at midnight ET. Re-run `auth` when you see a 401 error.

```bash
# Step 2: Sync all data and build the analytics layer
python -m etrade_sync sync

# Step 3: Seed reference tables (one-time, re-run when new symbols appear)
python -m etrade_sync seed-symbols   # sector/industry metadata via yfinance
python -m etrade_sync seed-dates     # calendar + NYSE trading day spine

# Step 4: Launch the dashboard
streamlit run dashboard/app.py
```

A full `sync` automatically runs the ledger rebuild, FIFO realized P/L matching, materialized view refresh, and position reconciliation after pulling data from E*TRADE.

---

## Usage

### Sync

```bash
# Sync everything (incremental, uses watermarks in sync_state)
python -m etrade_sync sync

# Sync a single account
python -m etrade_sync sync --account <account_id_key>

# Sync only one data type
python -m etrade_sync sync --only accounts
python -m etrade_sync sync --only balances
python -m etrade_sync sync --only positions
python -m etrade_sync sync --only transactions
python -m etrade_sync sync --only orders

# Historical range options (mutually exclusive; apply to transactions + orders only)
python -m etrade_sync sync --days 30            # last N days
python -m etrade_sync sync --from-beginning     # full 2-year history
python -m etrade_sync sync --from 2025-01-01    # from a specific date
python -m etrade_sync sync --from 2025-01-01 --to 2025-06-30
```

### Analytics layer

These run automatically after every full sync. Run manually after schema changes or to force a rebuild:

```bash
# Rebuild normalized ledger from transactions (incremental upsert)
python -m etrade_sync build-ledger

# Full rebuild — truncates ledger and dependent tables before repopulating
python -m etrade_sync build-ledger --full-rebuild

# FIFO cost basis matching — rebuilds realized_gains table
python -m etrade_sync build-realized-pnl

# Refresh all materialized views
python -m etrade_sync refresh-views

# Apply schema changes — drop + recreate all materialized views, re-run all SQL files
# Run this after any data_model/*.sql change. Table data is preserved.
python -m etrade_sync migrate

# Compare ledger reconstructed positions to latest API snapshot
python -m etrade_sync reconcile

# Collapse duplicate transaction_ingest_audit rows (safe to run anytime)
python -m etrade_sync cleanup-audit           # deletes duplicates
python -m etrade_sync cleanup-audit --dry-run # preview blast radius without deleting

# Seed symbol metadata (sector, industry, asset class) via yfinance
python -m etrade_sync seed-symbols

# Seed SPY benchmark price history via yfinance
python -m etrade_sync seed-prices

# Generate date spine (calendar + NYSE trading day flags)
python -m etrade_sync seed-dates
```

---

## Database Schema

All tables are created automatically on first sync. Schema definitions live in `data_model/` as numbered SQL files executed in order.

### Raw sync tables — `data_model/001_*.sql` – `007_*.sql`

| Table | Strategy | Key |
|---|---|---|
| `accounts` | Upsert | `account_id_key` |
| `balances` | Append (point-in-time snapshots) | — |
| `positions` | Append (point-in-time snapshots) | — |
| `transactions` | Upsert | `transaction_id` |
| `orders` | Upsert header + replace legs | `order_id` |
| `order_details` | Replaced on each order update | FK → `order_id` |
| `sync_state` | Watermarks per `(account_id_key, data_type)` | — |

### Pipeline monitoring — `data_model/008_*.sql` – `009_*.sql`

| Table | Description |
|---|---|
| `sync_log` | One row per pipeline run — job name, status, duration, row counts, triggered_by |
| `reconciliation_log` | Per-position comparison of ledger qty vs latest API snapshot |

### Ledger — `data_model/010_*.sql`

| Table | Description |
|---|---|
| `ledger` | Normalized event log — one row per financial event, signed quantities (positive=acquired, negative=disposed). Grain extended with `source_line_id` for future multi-leg order fan-out. |

### Dimension tables — `data_model/011_*.sql` – `020_*.sql`

| Table | Description |
|---|---|
| `dim_symbols` | Ticker metadata: CUSIP, sector, industry, asset class, vehicle_type, exchange (via yfinance) |
| `dim_sectors` | Canonical sector list with sort order — source of truth for the sector dropdown in the dashboard |
| `dim_symbol_overrides` | Manual sector / asset_class / vehicle_type overrides per symbol — takes precedence over yfinance in all views |
| `dim_dates` | Date spine 2013–2027: ISO week/year, NYSE trading day flag, fiscal period |
| `dim_accounts` | SCD Type 2 account history: `account_sk` surrogate, `effective_from/to`, `is_current`. `dim_accounts_current` view for current-state queries. |
| `symbol_exposure_tags` | Many-to-many thematic tags per symbol (Uranium, Precious Metals, Gold, Copper, etc.). A symbol can have multiple tags; values overlap intentionally. |

### Fact tables — `data_model/030_*.sql` – `032_*.sql`

| Table | Description |
|---|---|
| `fact_transactions` | One row per financial event keyed to dims |
| `fact_positions` | Point-in-time position snapshots, grain on `(account_id_key, symbol, fetched_at)` |
| `fact_cashflows` | Cash-only events: deposits, withdrawals, dividends, interest, fees |

### Analytics tables — `data_model/050_*.sql` – `058_*.sql`

| Table/View | Description |
|---|---|
| `mv_unrealized_pnl` | Materialized view: unrealized P/L from latest positions snapshot |
| `realized_gains` | FIFO-matched buy/sell lots with split-adjusted cost basis, proceeds, realized P/L, short/long term |
| `mv_portfolio_timeseries` | Daily portfolio value, returns, rolling metrics, drawdown — aggregated across all accounts |
| `mv_allocations` | Current sector / asset_class / vehicle_type breakdown with override priority: `dim_symbol_overrides` > yfinance. Includes `exposure_tags` array from `symbol_exposure_tags`. |
| `market_prices` | SPY daily close prices via yfinance — used for benchmark comparison |
| `mv_benchmark_comparison` | Portfolio return vs SPY: cumulative, rolling 30d, alpha — all-accounts |
| `mv_portfolio_timeseries_by_account` | Same as `mv_portfolio_timeseries` but with `account_id_key` as a dimension |
| `mv_attribution_timeseries` | Daily sector + asset class market value and P/L per account — used for attribution drill-downs |
| `mv_benchmark_comparison_by_account` | Portfolio vs SPY cumulative/rolling comparison per account |

---

## Sync Behavior

| Data type | Strategy | Notes |
|---|---|---|
| Accounts | Upsert | Re-run safe; `updated_at` tracks changes |
| Balances | Append | Each run adds a snapshot for trend tracking |
| Positions | Append | Each run adds a snapshot; query by `MAX(fetched_at)` for current |
| Transactions | Incremental | Watermark in `sync_state`; 2-year lookback on first run |
| Orders | Incremental | Header upsert + legs replaced on each sync |
| Ledger | Upsert from transactions | Auto-runs after every full sync |
| Realized P/L | Full rebuild | FIFO-matched; auto-runs after ledger rebuild |
| Materialized views | `REFRESH CONCURRENTLY` | Auto-runs after full sync |
| Reconciliation | Full rebuild | Compares ledger qty to API positions; auto-runs after full sync |

---

## Dashboard

```bash
streamlit run dashboard/app.py
```

| Page | Description |
|---|---|
| Pipeline Status | Token freshness alert, job run history, table health, last sync times. **Run Jobs** panel covers all batch commands (sync, migrate, refresh-views, build-ledger, build-realized-pnl, seed-symbols, seed-prices, seed-dates, reconcile, cleanup-audit) with live output streaming. |
| Portfolio Overview | Total account value, cash, invested capital, unrealized P/L. Account filter + position filters (symbol, sector, asset class). Three-column donut layout: Sector (risk assets only), Asset Class, Vehicle Type — all with inside-arc % labels. Theme exposure bar chart (overlap note). Full positions table with Vehicle Type and Themes columns. All KPIs and charts respond to active filters. |
| Performance | Equity curve vs SPY, drawdown, rolling 30d returns, rolling volatility. Account + Period + SPY toggle filters. Attribution drill-downs by Account (overlaid equity curves), Sector, and Asset Class (stacked area + unrealized P/L bar). Realized P/L by year with lot detail table. |
| Symbol Admin (P9) | Three tabs: **Symbol Overrides** — set sector, asset class, and vehicle type per symbol with notes; **Exposure Tags** — manage thematic tags (Uranium, Precious Metals, Copper, etc.) per symbol via multiselect; **Manage Sectors** — add custom sectors. All saves auto-refresh `mv_allocations`. |

Trading History (#27), Risk & Exposure (#28), and Strategy Tags (#29) are in progress.

---

## Automated Sync (macOS launchd)

| Job | Schedule | Script | Log |
|---|---|---|---|
| Daily | 6:00 AM every day | `scripts/sync_daily.sh` | `logs/sync_daily.log` |
| Weekly | 7:00 AM every Sunday | `scripts/sync_weekly.sh` | `logs/sync_weekly.log` |
| Auth reminder | 10:00 PM every day | `scripts/auth_reminder.sh` | — |

The daily job runs a full sync (all data types + ledger rebuild + realized P/L + view refresh + reconcile). The weekly job does a full 30-day history refresh.

### Install the launchd agents

```bash
cp scripts/com.fifthdragon.sync-daily.plist ~/Library/LaunchAgents/
cp scripts/com.fifthdragon.sync-weekly.plist ~/Library/LaunchAgents/
cp scripts/com.fifthdragon.auth-reminder.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fifthdragon.sync-daily.plist
launchctl load ~/Library/LaunchAgents/com.fifthdragon.sync-weekly.plist
launchctl load ~/Library/LaunchAgents/com.fifthdragon.auth-reminder.plist
```

### Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.fifthdragon.sync-daily.plist
launchctl unload ~/Library/LaunchAgents/com.fifthdragon.sync-weekly.plist
launchctl unload ~/Library/LaunchAgents/com.fifthdragon.auth-reminder.plist
```

### Token renewal (required daily)

**E*TRADE OAuth tokens expire every day at midnight Eastern Time.**

At **10:00 PM** each night, the auth reminder job checks if your token is stale. If it is, you'll get a macOS notification. Re-authenticate before midnight so the 6 AM sync runs cleanly:

```bash
python -m etrade_sync auth
```

This opens your browser to the E*TRADE authorization page. After approving, paste the verifier code. The new token is saved to `~/.config/etrade/tokens.json` (mode 600). If the sync scripts detect a stale or missing token at runtime, they send a notification and exit cleanly instead of hitting 401 errors.

---

## Sandbox vs Production

Set `ETRADE_DEV=true` in `.env` for the sandbox (`apisb.etrade.com`). Switch to `ETRADE_DEV=false` for production (`api.etrade.com`). Re-run `python -m etrade_sync auth` whenever you change environments.

Note: the E*TRADE sandbox orders endpoint is unreliable by design (intended for order placement testing only). Orders sync is fully verified against production.

---

## Verification

### Validate a full pipeline rebuild

Run this sequence from the **Pipeline Status → Run Jobs** panel (or CLI) after any schema change, CSV import, or code update to confirm the analytics layer is clean end-to-end:

| Step | Job | Option | Expected output |
|---|---|---|---|
| 1 | `cleanup-audit` | Dry run ✓ | `duplicate_groups: 0` — no audit rows to collapse |
| 2 | `build-ledger` | — | `backfilled provenance for 0 row(s)` · `upserted 899 row(s)` (or your count) |
| 3 | `build-realized-pnl` | — | FIFO lots matched, open lots saved |
| 4 | `refresh-views` | — | All materialized views refresh without error |
| 5 | `reconcile` | — | No unexpected discrepancies between ledger and API snapshot |

If `build-ledger` reports `backfilled provenance for N row(s)` with N > 0, new legacy rows were found and fixed automatically — normal after importing old CSV data.

If you run `build-ledger` without following it with `build-realized-pnl`, you will see:
```
ledger: cleared open_lots + realized_gains (run build_realized_pnl to restore)
```
This is expected — `open_lots` and `realized_gains` are intentionally cleared before the ledger is repopulated and must be rebuilt explicitly.

### SQL spot checks

```sql
-- Check for duplicate ledger rows (should always be 0)
SELECT account_id_key, event_timestamp, event_type, symbol, quantity, COUNT(*)
FROM ledger
WHERE source_table = 'transactions'
GROUP BY 1,2,3,4,5
HAVING COUNT(*) > 1;

-- Row counts across all tables
SELECT 'accounts'       AS tbl, count(*) FROM accounts
UNION ALL SELECT 'transactions', count(*) FROM transactions
UNION ALL SELECT 'ledger',       count(*) FROM ledger
UNION ALL SELECT 'realized_gains', count(*) FROM realized_gains;

-- Current unrealized P/L
SELECT symbol, quantity, cost_basis, market_value, unrealized_pnl, unrealized_pnl_pct
FROM mv_unrealized_pnl
ORDER BY unrealized_pnl DESC;

-- Realized gains/losses this year
SELECT symbol, buy_date, sell_date, quantity, cost_basis, proceeds, realized_pnl, term
FROM realized_gains
WHERE sell_date >= date_trunc('year', current_date)
ORDER BY sell_date;

-- Recent pipeline runs
SELECT job_name, status, started_at, duration_s, triggered_by
FROM sync_log
ORDER BY started_at DESC
LIMIT 10;
```
