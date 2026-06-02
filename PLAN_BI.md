# Fifth Dragon Capital — BI & Analytics Plan

## Architecture Overview

Four layers built on top of the raw E*TRADE sync pipeline:

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
│  Layer 0: Raw Sync (existing)           │  ← E*TRADE API → Postgres raw tables
│  accounts, balances, positions,         │
│  transactions, orders, order_details    │
└─────────────────────────────────────────┘
```

Raw tables are never modified by the BI layers — they stay as the source of truth.

---

## Phase 1 — Trading Ledger (Foundation)

**Why:** Current `transactions` table mixes event types (buy, sell, dividend, fee, transfer) without a
unified schema. Accurate P/L requires tracking every event precisely.

### `ledger` table

One row per financial event, normalized from `transactions` + `orders`:

```sql
CREATE TABLE ledger (
    id               SERIAL PRIMARY KEY,
    account_id_key   TEXT NOT NULL,
    event_date       TIMESTAMPTZ NOT NULL,
    event_type       TEXT NOT NULL,       -- see taxonomy below
    symbol           TEXT,               -- ticker or CUSIP
    security_type    TEXT,               -- EQ, OPTN, BOND, MF, CASH
    quantity         NUMERIC,            -- positive = acquired, negative = disposed
    price            NUMERIC,            -- per share/unit
    gross_amount     NUMERIC,            -- quantity × price
    net_amount       NUMERIC,            -- after fees/commissions
    fee              NUMERIC DEFAULT 0,
    currency         TEXT DEFAULT 'USD',
    source_table     TEXT,               -- 'transactions' or 'orders'
    source_id        TEXT,               -- transaction_id or order_id
    raw              JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_table, source_id)
);
```

### Event Taxonomy

Map raw E*TRADE `transaction_type` → normalized `event_type`:

| E*TRADE type | Ledger event_type | Notes |
|---|---|---|
| Bought | `buy` | quantity > 0 |
| Sold | `sell` | quantity < 0 |
| Dividend | `dividend` | income |
| Qualified Dividend | `dividend_qualified` | tax-advantaged |
| Interest Income | `interest` | cash income |
| Fee | `fee` | commission/fee |
| Transfer | `transfer` | cash in/out |
| Online Transfer | `deposit` or `withdrawal` | based on sign |
| Stock Split | `split` | adjust cost basis |
| Redemption | `redemption` | bond/fund maturity |
| Bill Payment | `withdrawal` | |
| POS | `withdrawal` | debit card |

### Derivable from ledger

Once populated, the ledger directly supports:
- Cost basis per position (sum of buys × price)
- Average entry price (total cost / total shares)
- Realized P/L (matched buy/sell pairs using FIFO)
- Net deposits (sum of deposits - withdrawals)
- Cash balance reconciliation

---

## Phase 2 — Analytical Schema (Star Schema)

Build dimensional model on top of the ledger for BI query performance.

### Fact Tables

#### `fact_transactions`
Derived from `ledger` — one row per trade/income event:
```sql
-- Columns: date_key, account_key, symbol_key, event_type,
--          quantity, price, gross_amount, net_amount, fee
```

#### `fact_positions`
Derived from raw `positions` snapshots — daily portfolio state:
```sql
-- Columns: date_key, account_key, symbol_key, quantity,
--          cost_basis, market_value, unrealized_pnl, unrealized_pnl_pct
```

#### `fact_cashflows`
Deposits, withdrawals, dividends, interest — cash-only events:
```sql
-- Columns: date_key, account_key, event_type, amount, running_balance
```

#### `fact_option_greeks` *(future)*
If options data becomes available via market API:
```sql
-- Columns: date_key, symbol_key, delta, gamma, theta, vega, iv
```

### Dimension Tables

#### `dim_accounts`
```sql
CREATE TABLE dim_accounts AS
SELECT account_id_key, account_id, account_name, account_mode,
       account_type, institution_type, status
FROM accounts;
```

#### `dim_symbols`
Ticker metadata — enriched from an external source (Yahoo Finance, etc.):
```sql
CREATE TABLE dim_symbols (
    symbol       TEXT PRIMARY KEY,
    name         TEXT,
    sector       TEXT,        -- Energy, Technology, Materials, etc.
    industry     TEXT,
    asset_class  TEXT,        -- Equity, Bond, ETF, Option, Cash
    currency     TEXT DEFAULT 'USD',
    exchange     TEXT
);
```

#### `dim_dates`
Standard date spine for time-series joins:
```sql
CREATE TABLE dim_dates (
    date_key     DATE PRIMARY KEY,
    year         INT,
    quarter      INT,
    month        INT,
    week         INT,
    day_of_week  INT,
    is_weekend   BOOL,
    is_trading_day BOOL
);
```

---

## Phase 2.5 — Reconciliation & Correctness

**Why:** Before building analytics, verify the pipeline produces numbers that agree with E*TRADE's own reported values. Catching discrepancies early avoids building dashboards on bad data.

### Checks to implement

| Check | Method |
|---|---|
| Cash balance | Calculated cash ledger (deposits − withdrawals + dividends + interest − buys + sells − fees) == API cash balance |
| Current positions | Reconstructed positions from ledger (cumulative quantity per symbol) == E*TRADE positions endpoint |
| Market value | Reconstructed shares × latest price == E*TRADE market value (within rounding) |
| Realized P/L | FIFO-computed realized P/L == E*TRADE tax documents where available |
| No duplicates | `(source_table, source_id)` unique constraint in ledger holds |
| Missing corporate actions | Splits/mergers that cause quantity gaps in ledger |

### First milestone

> "Given E*TRADE data, reconstruct current positions, cash balance, realized P/L, unrealized P/L, and daily portfolio value — and compare each against broker-reported values."

This milestone must pass before moving to Phase 3.

---

## Phase 3 — Intelligence Layer

Materialized views that derive the actual insights. Refreshed after each sync.

### A. Realized P/L (FIFO cost basis matching)

```sql
CREATE MATERIALIZED VIEW mv_realized_pnl AS
-- Match sell events to buy events using FIFO per symbol per account
-- Columns: account_id_key, symbol, open_date, close_date,
--          quantity, cost_basis, proceeds, realized_gain,
--          holding_days, short_term (< 1 year), long_term
```

Key outputs:
- Realized gain/loss per trade
- Short-term vs long-term classification (tax relevant)
- Holding period per lot

### B. Unrealized P/L

```sql
CREATE MATERIALIZED VIEW mv_unrealized_pnl AS
SELECT
    p.account_id_key,
    p.symbol,
    p.quantity,
    p.total_cost       AS cost_basis,
    p.market_value,
    p.market_value - p.total_cost AS unrealized_pnl,
    p.total_gain_pct   AS unrealized_pnl_pct,
    p.fetched_at       AS as_of
FROM positions p
WHERE p.fetched_at = (SELECT MAX(fetched_at) FROM positions);
```

### C. Trading Performance Metrics

```sql
CREATE MATERIALIZED VIEW mv_trading_performance AS
-- Per symbol, per account, rolling periods
-- Columns:
--   win_rate          (% of trades with realized gain > 0)
--   avg_win           (avg gain on winning trades)
--   avg_loss          (avg loss on losing trades)
--   expectancy        (win_rate × avg_win - loss_rate × avg_loss)
--   profit_factor     (gross_profit / gross_loss)
--   max_drawdown      (largest peak-to-trough loss)
--   avg_holding_days
```

### D. Allocation Analytics

```sql
CREATE MATERIALIZED VIEW mv_allocations AS
-- Join current positions → dim_symbols for sector/asset class
-- Columns:
--   sector, asset_class, market_value, pct_of_portfolio,
--   concentration_score
-- Highlights: uranium exposure, precious metals, tech, bonds, cash
```

### E. Benchmark Comparison

```sql
CREATE MATERIALIZED VIEW mv_benchmark_comparison AS
-- Join mv_portfolio_timeseries against a market_prices table (SPY daily close)
-- Columns: date, portfolio_return_pct, spy_return_pct, alpha,
--          rolling_30d_portfolio, rolling_30d_spy
```

Requires a market price history table (see Open Questions for data source).

### F. Time-Series Analytics

```sql
CREATE MATERIALIZED VIEW mv_portfolio_timeseries AS
-- From positions snapshots over time
-- Columns: date, total_market_value, total_cost_basis,
--          total_unrealized_pnl, daily_return_pct,
--          rolling_7d_return, rolling_30d_return,
--          rolling_90d_return, rolling_volatility_30d,
--          max_drawdown_pct
```

---

## Phase 3.5 — Strategy Tagging

**Why:** Reporting by trade type (swing vs long-term vs options play) is far more actionable than aggregate stats. A single tag per trade unlocks P/L, win rate, capital deployed, and holding period broken out by strategy.

### `trade_tags` table

```sql
CREATE TABLE trade_tags (
    id              SERIAL PRIMARY KEY,
    source_table    TEXT NOT NULL,   -- 'ledger' or 'orders'
    source_id       TEXT NOT NULL,   -- ledger.source_id or orders.order_id
    strategy        TEXT NOT NULL,   -- see taxonomy below
    notes           TEXT,
    tagged_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_table, source_id)
);
```

### Strategy taxonomy

| Tag | Description |
|---|---|
| `swing_trade` | Short-term directional, days to weeks |
| `long_term` | Buy and hold, months to years |
| `earnings_play` | Position entered around earnings event |
| `covered_call` | Short call against long equity |
| `cash_secured_put` | Short put with cash collateral |
| `hedge` | Reduces portfolio risk |
| `dividend` | Held primarily for income |
| `rebalance` | Portfolio allocation adjustment |

Tags can be added via a simple Streamlit form on the Transaction History page or via direct SQL. Programmatic tagging (e.g., auto-tag options by type) is a later enhancement.

### Metrics unlocked by tagging

- P/L by strategy
- Win rate by strategy
- Average holding period by strategy
- Capital deployed by strategy over time

---

## Phase 4 — BI Layer

### Tool: Streamlit *(recommended)*

**Why Streamlit for this project:**
- Python-first — same language as the pipeline
- Lives in this repo as `dashboard/`
- Runs locally, no server needed
- Can be deployed to Streamlit Cloud later for mobile access
- Full control over layout and calculations

### Dashboard Pages

#### 1. Portfolio Overview
- Total account value, cash, invested capital
- Unrealized P/L today vs cost basis
- Margin usage (if applicable)
- Allocation pie: sector / asset class breakdown
- Exposure by ticker (top N)
- Top 10 positions by market value

#### 2. Performance
- Equity curve (cumulative return from position snapshots)
- Daily/weekly/monthly return table
- Rolling 30/90-day return chart vs SPY benchmark
- Volatility (rolling 30-day)
- Max drawdown chart with drawdown periods highlighted
- Realized P/L by year/quarter

#### 3. Trading History
- Win rate, avg win / avg loss, profit factor
- Realized P/L by ticker
- P/L by strategy tag (requires Phase 3.5)
- P/L by holding period bucket (<1 week, 1w–1m, 1m–1y, >1y)
- Commissions/fees impact over time
- Monthly P/L heatmap (year × month grid)
- Trade scatterplot: return % vs holding days
- Searchable/filterable ledger with strategy tag form

#### 4. Account Detail
- Per-account breakdown
- Position-level detail with cost basis and unrealized P/L
- Order history

#### 5. Risk & Exposure
- Sector concentration bar chart
- Uranium/energy/precious metals exposure (given your holdings)
- Options exposure (delta-adjusted if greeks available)
- Largest losing trades
- Short-term vs long-term holding breakdown
- Position sizing relative to portfolio %

---

## Build Sequence

| Step | Phase | Task | Estimated effort |
|---|---|---|---|
| 1 | ✅ Done | Raw sync pipeline (accounts, balances, positions, transactions, orders) | — |
| 2 | ✅ Done | Scheduling (daily/weekly launchd + auth reminder) | — |
| 3 | Ledger | Build `ledger` table + populate from transactions | 2-3 hours |
| 4 | Dimensions | `dim_symbols` (manual seed for your holdings) | 1 hour |
| 5 | Reconcile | Reconstruct positions from ledger; compare to API positions | 2-3 hours |
| 6 | Intelligence | `mv_unrealized_pnl`, `mv_realized_pnl` (FIFO) | 3-4 hours |
| 7 | Intelligence | `mv_portfolio_timeseries`, `mv_allocations` | 2-3 hours |
| 8 | Intelligence | `mv_benchmark_comparison` (requires market price history) | 2 hours |
| 9 | BI | Streamlit scaffold + Portfolio Overview page | 3-4 hours |
| 10 | BI | Performance + Trading History pages | 3-4 hours |
| 11 | BI | Risk & Exposure page | 2 hours |
| 12 | Tagging | Strategy tag table + tag form in Trading History page | 2-3 hours |
| 13 | BI | P/L by strategy, win rate by strategy charts | 2 hours |
| 14 | Future | Tax-lot and wash-sale-aware reporting | TBD |

**Total estimated for steps 3–13:** ~25 hours of focused development

---

## Open Questions

- [ ] FIFO vs average cost for P/L calculation? (FIFO is US tax standard)
- [ ] Do you want tax lot tracking per trade?
- [ ] Source for `dim_symbols` sector/industry data? (Yahoo Finance API is free)
- [ ] Source for market price history for benchmark comparison? (Yahoo Finance `yfinance` library is simplest)
- [ ] Deploy Streamlit locally only, or to cloud for mobile access?
- [ ] How frequently refresh materialized views? (after each sync, or on-demand?)
- [ ] Strategy tags: manual only, or also auto-tag options by contract type?
