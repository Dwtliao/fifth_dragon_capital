# Fifth Dragon Capital — E*TRADE Data Pipeline

A Python CLI that syncs your E*TRADE account data to a local Postgres database. Pulls accounts, balances, positions, transactions, and orders on demand.

---

## Prerequisites

- Python 3.12+
- PostgreSQL running locally
- E*TRADE API credentials ([developer.etrade.com](https://developer.etrade.com))

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
# Step 2: Sync all data
python -m etrade_sync sync
```

---

## Usage

```bash
# Sync everything (incremental, uses watermarks)
python -m etrade_sync sync

# Sync a single account
python -m etrade_sync sync --account <account_id_key>

# Sync only one data type
python -m etrade_sync sync --only accounts
python -m etrade_sync sync --only balances
python -m etrade_sync sync --only positions
python -m etrade_sync sync --only transactions
python -m etrade_sync sync --only orders

# Historical range options (mutually exclusive)
python -m etrade_sync sync --days 30            # last N days
python -m etrade_sync sync --from-beginning     # full 2-year history
python -m etrade_sync sync --from 2025-01-01    # from a specific date
python -m etrade_sync sync --from 2025-01-01 --to 2025-06-30  # date range
```

### Analytics layer

After syncing, populate the BI foundation tables:

```bash
# Build normalized event ledger from transactions (incremental upsert)
python -m etrade_sync build-ledger

# Full rebuild — truncates ledger and dependent fact tables, then repopulates
python -m etrade_sync build-ledger --full-rebuild

# Seed symbol metadata (sector, industry, asset class) via yfinance
python -m etrade_sync seed-symbols

# Generate date spine (calendar + trading day flags)
python -m etrade_sync seed-dates
```

---

## Database Schema

### Raw sync tables (Layer 0)

7 tables populated by the sync pipeline:

| Table | Type | Key |
|---|---|---|
| `accounts` | upsert | `account_id_key` |
| `balances` | append (point-in-time snapshots) | — |
| `positions` | append (point-in-time snapshots) | — |
| `transactions` | upsert | `transaction_id` |
| `orders` | upsert header | `order_id` |
| `order_details` | replace legs on update | FK → `order_id` |
| `sync_state` | watermarks | `(account_id_key, data_type)` |

Tables are created automatically on first sync. Schema definitions live in `data_model/001_*.sql` through `007_*.sql`.

### Analytics tables (Layer 1–2)

Built on top of the raw sync tables:

| Table | Layer | Description |
|---|---|---|
| `ledger` | 1 | Normalized event log — one row per financial event, signed quantities |
| `dim_symbols` | 2 | Ticker metadata: sector, industry, asset class (seeded via yfinance) |
| `dim_dates` | 2 | Date spine with ISO week/year and NYSE trading day flag |
| `dim_accounts` | 2 | Account dimension with SCD Type 2 history + `dim_accounts_current` view |

Schema definitions: `data_model/010_*.sql` through `020_*.sql`.

### Fact tables (Layer 2 — draft, not yet built)

| Table | Description |
|---|---|
| `fact_transactions` | One row per financial event, keyed to dims |
| `fact_positions` | Point-in-time position snapshots, grain enforced on (account, symbol, fetched_at) |
| `fact_cashflows` | Cash-only events (deposits, withdrawals, dividends, interest, fees) |

Schema definitions: `data_model/030_*.sql` through `032_*.sql`.

---

## Verification

Connect with `psql $DATABASE_URL` and run:

```sql
-- Row counts across all tables
SELECT 'accounts'      AS tbl, count(*) FROM accounts
UNION ALL SELECT 'balances',      count(*) FROM balances
UNION ALL SELECT 'positions',     count(*) FROM positions
UNION ALL SELECT 'transactions',  count(*) FROM transactions
UNION ALL SELECT 'orders',        count(*) FROM orders
UNION ALL SELECT 'order_details', count(*) FROM order_details;

-- Latest positions by market value
SELECT symbol, security_type, quantity, market_value, total_gain_pct
FROM positions
WHERE fetched_at = (SELECT MAX(fetched_at) FROM positions)
ORDER BY market_value DESC NULLS LAST
LIMIT 20;

-- Recent transactions
SELECT transaction_date, transaction_type, description, amount, symbol
FROM transactions
ORDER BY transaction_date DESC NULLS LAST
LIMIT 20;

-- Orders with legs
SELECT o.order_id, o.order_type, o.status, o.placed_time,
       d.symbol, d.order_action, d.ordered_quantity, d.security_type
FROM orders o
JOIN order_details d ON d.order_id = o.order_id
ORDER BY o.placed_time DESC NULLS LAST;
```

---

## Sync Behavior

| Data type | Strategy | Notes |
|---|---|---|
| Accounts | Upsert | Re-run safe; `updated_at` tracks changes |
| Balances | Append | Each run adds a new snapshot for trend tracking |
| Positions | Append | Each run adds a new snapshot; query by `MAX(fetched_at)` for current |
| Transactions | Incremental | Watermark in `sync_state`; 2-year lookback on first run |
| Orders | Incremental | Header upsert + legs replaced on each sync |

---

## Automated Sync (macOS launchd)

Two scheduled jobs run automatically via macOS launchd:

| Job | Schedule | Script | Log |
|---|---|---|---|
| Daily | 6:00 AM every day | `scripts/sync_daily.sh` | `logs/sync_daily.log` |
| Weekly | 7:00 AM every Sunday | `scripts/sync_weekly.sh` | `logs/sync_weekly.log` |
| Auth reminder | 10:00 PM every day | `scripts/auth_reminder.sh` | — |

The daily job syncs accounts, balances, positions, transactions, and orders. The weekly job does a full 30-day history refresh.

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

At **10:00 PM** each night, the auth reminder job checks if your token is stale. If it is, you'll get a macOS notification: **"Re-authenticate tonight."** Re-authenticate before midnight and the 6 AM sync will run without issues:

```bash
python -m etrade_sync auth
```

This opens your browser to the E*TRADE authorization page. After approving, paste the verifier code. The new token is saved to `~/.config/etrade/tokens.json` (mode 600). If you already re-authenticated that day, the 10 PM reminder stays silent.

If the sync scripts detect a stale or missing token at runtime, they send a notification and exit cleanly instead of failing with 401 errors.

---

## Sandbox vs Production

Set `ETRADE_DEV=true` in `.env` for the sandbox (`apisb.etrade.com`). Switch to `ETRADE_DEV=false` for production (`api.etrade.com`). Re-run `python -m etrade_sync auth` whenever you change environments.

Note: the E*TRADE sandbox orders endpoint is unreliable by design (intended for order placement testing only). Orders sync is fully verified against production.
