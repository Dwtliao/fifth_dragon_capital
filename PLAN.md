# Fifth Dragon Capital — E\*TRADE Data Pipeline

## Goal

Build a Python CLI tool that authenticates with E\*TRADE via OAuth1, pulls personal financial
data using the [pyetrade](https://github.com/jessecooper/pyetrade) library, and persists it
into a local Postgres database. The result is a reliable, repeatable sync that keeps local
tables up to date as new transactions, orders, and position snapshots arrive.

---

## pyetrade Library Overview

**Version:** 2.1.1 — installed at `~/git_repos/py312/venv/lib/python3.12/site-packages/pyetrade/`

### Available APIs

| Module | Class | What it provides |
|---|---|---|
| `authorization` | `ETradeOAuth` | OAuth1 token acquisition (request token → verifier → access token) |
| `authorization` | `ETradeAccessManager` | Token renewal and revocation |
| `accounts` | `ETradeAccounts` | Accounts, balances, portfolio positions, transaction history |
| `market` | `ETradeMarket` | Quotes, option chains, product lookup |
| `order` | `ETradeOrder` | List orders, place/preview/cancel equity + option orders |
| `alerts` | `ETradeAlerts` | List, read, delete user alerts |

### Key Methods We'll Use

```python
# Accounts
accounts.list_accounts()                              # all linked accounts
accounts.get_account_balance(account_id_key)          # cash + net value
accounts.get_account_portfolio(account_id_key,        # positions (paginated)
    page_number=1, count=50)
accounts.list_transactions(account_id_key,            # tx history, marker-based
    start_date, end_date, count=50, marker=None)
accounts.list_transaction_details(account_id_key,     # single tx detail
    transaction_id)

# Orders
order.list_orders(account_id_key, count=100,          # orders, marker-based
    marker=None)
```

### Auth Flow (OAuth1)

```
1. Create ETradeOAuth(consumer_key, consumer_secret)
2. Call .get_request_token() → prints authorization URL
3. User visits URL, authorizes, copies the verifier code
4. Call .get_access_token(verifier) → returns {oauth_token, oauth_token_secret}
5. Save tokens to ~/.config/etrade/tokens.json
6. On subsequent runs: load saved tokens (renew if needed via ETradeAccessManager)
```

### Rate Limits & Pagination Notes

- Quotes: max 25 symbols per call (not needed for this pipeline)
- Portfolio positions: `page_number` + `count=50` per page
- Transactions: `marker`-based cursor; 2-year lookback max
- Orders: `marker`-based cursor; max 100 per request
- Alerts: max 300 per request

---

## Project Structure

```
fifth_dragon_capital/
├── PLAN.md                    ← this file
├── README.md
├── .env.example               ← template for required env vars
├── requirements.txt
├── etrade_sync/
│   ├── __init__.py
│   ├── config.py              # load + validate env vars
│   ├── auth.py                # OAuth flow, token file storage + renewal
│   ├── db.py                  # Postgres connection, CREATE TABLE IF NOT EXISTS
│   └── sync/
│       ├── __init__.py
│       ├── accounts.py        # sync_accounts(), sync_balances()
│       ├── positions.py       # sync_positions() — paginated full refresh
│       ├── transactions.py    # sync_transactions() — incremental by watermark
│       └── orders.py          # sync_orders() — incremental by watermark
└── main.py                    # CLI: python -m etrade_sync [auth|sync]
```

---

## Postgres Schema

Schema fields derived from live sandbox API responses (see sandbox_data/).

```sql
-- Static account metadata; upsert on account_id_key
-- Source: AccountListResponse.Accounts.Account[]
CREATE TABLE IF NOT EXISTS accounts (
    id               SERIAL PRIMARY KEY,
    account_id_key   TEXT UNIQUE NOT NULL,  -- accountIdKey
    account_id       TEXT,                  -- accountId
    account_name     TEXT,                  -- accountName (nickname)
    account_desc     TEXT,                  -- accountDesc
    account_mode     TEXT,                  -- accountMode: IRA, CASH, etc.
    account_type     TEXT,                  -- accountType: MARGIN, INDIVIDUAL, CASH
    institution_type TEXT,                  -- institutionType: BROKERAGE
    status           TEXT,                  -- accountStatus: ACTIVE, CLOSED
    closed_date      TIMESTAMPTZ,           -- closedDate (epoch ms, 0 if active)
    raw              JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Point-in-time balance snapshots (append per sync run)
-- Source: BalanceResponse.Computed + BalanceResponse.Computed.RealTimeValues
CREATE TABLE IF NOT EXISTS balances (
    id                          SERIAL PRIMARY KEY,
    account_id_key              TEXT NOT NULL,
    fetched_at                  TIMESTAMPTZ DEFAULT NOW(),
    cash_available_for_invest   NUMERIC,    -- Computed.cashAvailableForInvestment
    cash_available_for_withdraw NUMERIC,    -- Computed.cashAvailableForWithdrawal
    net_cash                    NUMERIC,    -- Computed.netCash
    cash_balance                NUMERIC,    -- Computed.cashBalance
    total_account_value         NUMERIC,    -- Computed.RealTimeValues.totalAccountValue
    net_mv                      NUMERIC,    -- Computed.RealTimeValues.netMv (market value)
    net_mv_long                 NUMERIC,    -- Computed.RealTimeValues.netMvLong
    raw                         JSONB
);

-- Point-in-time position snapshots (append per sync run)
-- Source: PortfolioResponse.AccountPortfolio[].Position[]
CREATE TABLE IF NOT EXISTS positions (
    id               SERIAL PRIMARY KEY,
    account_id_key   TEXT NOT NULL,
    fetched_at       TIMESTAMPTZ DEFAULT NOW(),
    position_id      BIGINT,                -- positionId
    symbol           TEXT,                  -- Product.symbol
    symbol_desc      TEXT,                  -- symbolDescription
    security_type    TEXT,                  -- Product.securityType: EQ, OPTN, MF, MMF
    position_type    TEXT,                  -- positionType: LONG, SHORT
    quantity         NUMERIC,               -- quantity (negative for SHORT)
    cost_per_share   NUMERIC,               -- costPerShare
    total_cost       NUMERIC,               -- totalCost
    market_value     NUMERIC,               -- marketValue
    total_gain       NUMERIC,               -- totalGain
    total_gain_pct   NUMERIC,               -- totalGainPct
    days_gain        NUMERIC,               -- daysGain
    days_gain_pct    NUMERIC,               -- daysGainPct
    pct_of_portfolio NUMERIC,               -- pctOfPortfolio
    raw              JSONB
);

-- Deduplicated transaction history; upsert on transaction_id
-- Source: TransactionListResponse.Transaction[]
-- transactionDate is Unix epoch seconds; converted to TIMESTAMPTZ on insert
CREATE TABLE IF NOT EXISTS transactions (
    id               SERIAL PRIMARY KEY,
    account_id_key   TEXT NOT NULL,
    transaction_id   TEXT UNIQUE NOT NULL,  -- transactionId
    transaction_date TIMESTAMPTZ,           -- from transactionDate (epoch secs)
    transaction_type TEXT,                  -- transactionType: Transfer, Fee, POS, Bill Payment, Sold, Bought
    description      TEXT,                  -- description
    description2     TEXT,                  -- description2 (optional reference number)
    amount           NUMERIC,               -- amount (negative = debit)
    symbol           TEXT,                  -- brokerage.displaySymbol (blank for non-trades)
    quantity         NUMERIC,               -- brokerage.quantity
    price            NUMERIC,               -- brokerage.price
    fee              NUMERIC,               -- brokerage.fee
    settlement_date  TIMESTAMPTZ,           -- brokerage.settlementDate (epoch secs, 0 if none)
    raw              JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Order headers; upsert on order_id
-- One row per orderId regardless of how many legs
-- Source: OrdersResponse.Order[]
CREATE TABLE IF NOT EXISTS orders (
    id                  SERIAL PRIMARY KEY,
    account_id_key      TEXT NOT NULL,
    order_id            BIGINT UNIQUE NOT NULL,  -- orderId
    order_type          TEXT,                    -- orderType: EQ, OPTN, SPREADS, ONE_CANCELS_ALL, etc.
    total_order_value   NUMERIC,                 -- totalOrderValue
    total_commission    NUMERIC,                 -- totalCommission
    placed_time         TIMESTAMPTZ,             -- OrderDetail[0].placedTime (epoch ms)
    status              TEXT,                    -- OrderDetail[0].status: OPEN, EXECUTED, CANCELLED, etc.
    raw                 JSONB,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Order detail legs; one row per Instrument within each OrderDetail
-- Supports multi-leg orders (spreads, OCA, options combos)
-- Source: OrdersResponse.Order[].OrderDetail[].Instrument[]
CREATE TABLE IF NOT EXISTS order_details (
    id                      SERIAL PRIMARY KEY,
    order_id                BIGINT NOT NULL REFERENCES orders(order_id),
    order_number            INT,                 -- OrderDetail.orderNumber (for OCA legs)
    symbol                  TEXT,                -- Product.symbol
    symbol_desc             TEXT,                -- symbolDescription
    security_type           TEXT,                -- Product.securityType: EQ, OPTN
    order_action            TEXT,                -- orderAction: BUY, SELL, BUY_OPEN, etc.
    price_type              TEXT,                -- priceType: MARKET, LIMIT, NET_DEBIT, etc.
    order_term              TEXT,                -- orderTerm: GOOD_FOR_DAY, etc.
    limit_price             NUMERIC,             -- limitPrice
    stop_price              NUMERIC,             -- stopPrice
    status                  TEXT,                -- status per detail line
    placed_time             TIMESTAMPTZ,         -- placedTime (epoch ms)
    executed_time           TIMESTAMPTZ,         -- executedTime (epoch ms, null if not executed)
    ordered_quantity        NUMERIC,             -- orderedQuantity
    filled_quantity         NUMERIC,             -- filledQuantity
    avg_execution_price     NUMERIC,             -- averageExecutionPrice
    estimated_commission    NUMERIC,             -- estimatedCommission
    call_put                TEXT,                -- Product.callPut: CALL, PUT (options only)
    expiry_year             INT,                 -- Product.expiryYear
    expiry_month            INT,                 -- Product.expiryMonth
    expiry_day              INT,                 -- Product.expiryDay
    strike_price            NUMERIC,             -- Product.strikePrice
    raw                     JSONB,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Watermarks for incremental sync
CREATE TABLE IF NOT EXISTS sync_state (
    id               SERIAL PRIMARY KEY,
    account_id_key   TEXT NOT NULL,
    data_type        TEXT NOT NULL,  -- 'transactions' or 'orders'
    last_synced_at   TIMESTAMPTZ,
    last_marker      TEXT,           -- pagination cursor for resuming
    UNIQUE (account_id_key, data_type)
);
```

---

## Environment Variables

```bash
# .env.example

# E*TRADE API credentials (from developer portal: https://developer.etrade.com)
ETRADE_CONSUMER_KEY=
ETRADE_CONSUMER_SECRET=

# true = sandbox (apisb.etrade.com), false = production (api.etrade.com)
ETRADE_DEV=false

# OAuth token file location (created automatically after `etrade_sync auth`)
ETRADE_TOKEN_FILE=~/.config/etrade/tokens.json

# Postgres connection string
DATABASE_URL=postgresql://postgres@localhost:5432/davidliao
```

---

## CLI Usage

```bash
# Step 1 (first run only): complete OAuth dance
python -m etrade_sync auth

# Step 2: sync all accounts, all data types
python -m etrade_sync sync

# Sync a single account
python -m etrade_sync sync --account <account_id_key>

# Sync only specific data types
python -m etrade_sync sync --only transactions
python -m etrade_sync sync --only positions
```

---

## Implementation Details

### Incremental Sync (transactions + orders)

- On each run, read `sync_state` for the latest watermark date per account
- If no watermark: fetch the maximum lookback (2 years for transactions)
- Upsert records by `transaction_id` / `order_id` — safe to re-run
- After a successful sync, update `sync_state.last_synced_at`

### Full Refresh (balances + positions)

- Always fetch all records on each run (they are point-in-time snapshots)
- Append to the table with `fetched_at` timestamp — no deduplication needed
- Useful for tracking portfolio drift over time

### Token Renewal

```python
# auth.py
def load_or_refresh_tokens():
    tokens = load_tokens_from_file()  # ~/.config/etrade/tokens.json
    if tokens_expired(tokens):
        mgr = ETradeAccessManager(consumer_key, consumer_secret,
                                   tokens['oauth_token'], tokens['oauth_token_secret'])
        mgr.renew_access_token()  # extends by 2 hours
    return tokens
```

### Pagination Pattern (transactions example)

```python
def sync_transactions(accounts_client, account_id_key, start_date, end_date):
    marker = None
    while True:
        resp = accounts_client.list_transactions(
            account_id_key, start_date=start_date, end_date=end_date,
            count=50, marker=marker)
        
        txns = resp.get('TransactionListResponse', {}).get('Transaction', [])
        if not txns:
            break
        
        upsert_transactions(txns, account_id_key)
        
        marker = resp.get('TransactionListResponse', {}).get('marker')
        if not marker:
            break
        time.sleep(0.2)  # courtesy delay
```

---

## Dependencies

```
pyetrade>=2.1.1
psycopg2-binary>=2.9
python-dotenv>=1.0
```

---

## Verification

After running `python -m etrade_sync sync` (connect with `psql postgresql://postgres@localhost:5432/davidliao`):

```sql
-- Check record counts
SELECT 'accounts' AS tbl, count(*) FROM accounts
UNION ALL SELECT 'balances', count(*) FROM balances
UNION ALL SELECT 'positions', count(*) FROM positions
UNION ALL SELECT 'transactions', count(*) FROM transactions
UNION ALL SELECT 'orders', count(*) FROM orders;

-- Latest positions by value
SELECT symbol, quantity, market_value, total_gain_pct
FROM positions
WHERE fetched_at = (SELECT MAX(fetched_at) FROM positions)
ORDER BY market_value DESC
LIMIT 20;

-- Recent transactions
SELECT transaction_date, category, symbol, quantity, price, amount
FROM transactions
ORDER BY transaction_date DESC
LIMIT 20;
```

---

## Open Questions / Next Steps

- [ ] Confirm Postgres is running locally and `DATABASE_URL` is set
- [ ] Obtain E*TRADE developer API key/secret from https://developer.etrade.com
- [ ] Decide whether to target sandbox first or go straight to production
- [ ] Consider whether alerts and market data quotes are in scope
- [ ] Add logging (structlog or standard logging) once core sync works
- [ ] Consider scheduled runs (launchd on macOS, or a simple cron job)
