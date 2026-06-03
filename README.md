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

# Refresh all materialized views (mv_unrealized_pnl, ...)
python -m etrade_sync refresh-views

# Compare ledger reconstructed positions to latest API snapshot
python -m etrade_sync reconcile

# Seed symbol metadata (sector, industry, asset class) via yfinance
python -m etrade_sync seed-symbols

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
| `dim_symbols` | Ticker metadata: CUSIP, sector, industry, asset class, exchange (via yfinance) |
| `dim_dates` | Date spine 2013–2027: ISO week/year, NYSE trading day flag, fiscal period |
| `dim_accounts` | SCD Type 2 account history: `account_sk` surrogate, `effective_from/to`, `is_current`. `dim_accounts_current` view for current-state queries. |

### Fact tables — `data_model/030_*.sql` – `032_*.sql`

| Table | Description |
|---|---|
| `fact_transactions` | One row per financial event keyed to dims |
| `fact_positions` | Point-in-time position snapshots, grain on `(account_id_key, symbol, fetched_at)` |
| `fact_cashflows` | Cash-only events: deposits, withdrawals, dividends, interest, fees |

### Analytics tables — `data_model/050_*.sql` – `051_*.sql`

| Table/View | Description |
|---|---|
| `mv_unrealized_pnl` | Materialized view: unrealized P/L from latest positions snapshot |
| `realized_gains` | FIFO-matched buy/sell lots with cost basis, proceeds, realized P/L, holding period, short/long term classification |

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
| Pipeline Status | Token freshness alert, last sync status, job run history, table health, last sync times, Run Sync Now button with live output streaming |

More pages (portfolio overview, performance, trading history, risk/exposure) are in progress.

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

```sql
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
