# Fifth Dragon Capital — E\*TRADE Data Pipeline

> The June 2, 2026 plan below is historical and preserved for reference. Its unchecked
> tasks, assumptions, and dependency versions are not the current implementation status.
> The active work is the [P7 alert-management execution plan](#current-plan-p7-alert-management)
> added October 8, 2026.

## Historical plan — June 2, 2026

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

---

## Current plan: P7 alert management

Date: October 8, 2026. Status: Priority 1 implemented and locally migrated; awaiting user
browser acceptance. Priorities 2–4 have not started. Development branch:
`feature/p7-alert-management`, created from `main`. Changes are not committed or merged.

### Objective and scope

Make price alerts easy to understand and manage without losing the automatic maintenance
provided by positions, watch levels, and trading journals. Fix lifecycle behavior before
reorganizing the UI: a cleaner screen is not sufficient if the next brief regenerates an
alert the user deliberately paused.

Deliver a dedicated Alerts workspace while retaining alert overlays in P7 Market Monitor.
Keep deterministic local logic for filtering, ranking, lifecycle, and notification handling.
After those foundations are accepted, optionally reuse the existing Morning Brief LLM call
to propose alert updates. No additional market-data provider is required; an LLM suggestion
must never directly mutate an alert.

Out of scope: trading/order execution, changing the Morning Brief analysis model, replacing
E*TRADE sync, automatic deletion of old alerts, and automatically changing investment levels.
Existing manual alerts and source records must be preserved unless explicitly selected for
a confirmed operation.

### Priority order and reviewable task list

Implement and validate each priority before expanding to the next. The baseline and backup
work is a prerequisite, not a separate feature release.

1. **Priority 1 — Dependable controls and polling (Phases 2–4).**
   - [x] Make Pause/Snooze/Resume durable across brief and journal reconciliation.
   - [x] Enforce expiration and shared eligibility in poller, main status tables, and overlays.
   - [x] Separate condition state from delivery success; expose failures and prevent
     concurrent workers from claiming the same notification.
2. **Priority 2 — Everyday management (Phases 5–6).**
   - [ ] Replace long dropdowns with searchable/selectable rows and a detail editor.
   - [ ] Display current price, distance, source, lifecycle, expiry, and poll health.
   - [ ] Provide safe manual creation/editing and confirmed scoped bulk actions.
   - [ ] Keep P7 charts/overlays, moving detailed management to an Alerts workspace.
3. **Priority 3 — Opt-in brief assistance (Phases 7–8).**
   - [ ] Default manual alerts to Manual / locked; allow explicit Brief-assisted opt-in.
   - [ ] Generate suggestions only from explicit, cited, fresh, direction-compatible levels.
   - [ ] Show an Accept / Keep / Snooze review queue with before/after values and reasons.
   - [ ] Validate and apply accepted changes locally with concurrency checks and history.
4. **Priority 4 — Source-following automation (deferred; separate approval).**
   - [ ] Evaluate an explicit Follow a source mode only after suggestions prove useful.
   - [ ] Follow a named structured watch level or position stop, never a prose mention.
   - [ ] Require opt-in, reversible changes, history, and a separately reviewed rollout.

Release A contains Priorities 1–2 and can ship without LLM-assisted work. Release B adds
Priority 3 after Release A acceptance. Apply the regression and rollout gates in Phases
9–10 to each release. Priority 4 is not part of either release's definition of done.

### Evidence and starting point

The October 8 read-only review found 81 alert records: 61 active and 20 archived. By source:

| Source | Total | Active | Archived |
|---|---:|---:|---:|
| Journal sync | 13 | 1 | 12 |
| Watch levels | 56 | 55 | 1 |
| Manual | 12 | 5 | 7 |

There were 27 triggered records across the complete inventory and one exact duplicate
group. These are a dated baseline, not fixed acceptance counts; refresh the audit before
implementation because normal pipeline activity changes them.

Primary code paths to review and change:

- `dashboard/pages/P7_Market_Monitor.py`: mixed charts/management UI, direct SQL mutations,
  separate long dropdowns, and poll output lost on rerun.
- `morning_brief/alert_compiler.py`: structural/journal reconciliation, expiration,
  duplicate detection, and manual-alert review reports.
- `alerts/poller.py` and `alerts/notify.py`: eligibility, trigger/rearm behavior, and delivery.
- `dashboard/alert_sorting.py` and `dashboard/alert_badges.py`: shared presentation logic.
- `data_model/`: existing alert schema and migration conventions.
- Existing alert tests and scheduled-poller definitions under `scripts/`.

Observed lifecycle problems to address:

1. Compiler reconciliation can undo direct threshold edits and re-enable disabled or
   archived source-managed alerts. Deleted managed alerts can be recreated.
2. Poller eligibility currently checks `enabled`, but not expiration or archival state.
3. Trigger state is recorded even when email delivery fails or is not configured.
4. Concurrent manual and scheduled polls can send duplicate notifications.
5. The UI conflates disabled and archived states, omits quote distance and expiry, and
   manages rows through a dropdown unrelated to the ranked table.
6. Chart-fragment refreshes can retain an old alert snapshot; poll failures/output are
   not persistently visible in the page.

### Operating rules and decisions

- Separate source-owned configuration from user-owned lifecycle controls. A position stop
  or watch level must be edited at its source, not silently overridden in `price_alerts`.
- Persist Pause/Snooze independently of compiler-owned fields. For managed alerts, use
  stable source identity (`source`, `source_key`) so suppression survives reconciliation
  and row recreation. Define a stable identity for manual alerts as well.
- Pause means indefinite suppression until explicit resume. Snooze means suppression until
  a chosen timestamp. Neither changes the trading level or deletes source information.
- Distinguish condition satisfied, notification event, and delivery result. A triggered
  condition does not prove that an email was sent.
- Preserve current strict `above`/`below` comparisons unless a separately approved change
  is needed. Define alerts as condition-based, not guaranteed witnessed price crossings.
- Default resume behavior: an eligible resumed alert may notify on the next poll if its
  condition is already satisfied. Explain this before confirmation and test it explicitly.
- Eligibility must be shared across poller, tables, and overlays: enabled, not archived,
  unexpired, and not paused/snoozed. Missing or invalid quotes never imply a crossing.
- Do not repurpose `pinned` as a pause flag. Do not automatically archive manual macro or
  futures alerts merely because the ticker is absent from portfolio positions.
- Retain source-aware duplicate checks; nearby levels are not necessarily duplicates.
- All bulk operations require previewed exact targets and explicit confirmation. Avoid
  defaulting destructive actions to all rows or all filtered records.
- No live emails, scheduled-job changes, or production-data cleanup during development tests.
- A symbol mention alone never authorizes an alert update. An explicit resistance level
  cannot replace a downside support alert merely because both refer to the same symbol.
- Existing manual alerts stay locked by default. Assistance changes ownership only through
  explicit user opt-in; skipping assistance or an LLM failure leaves alerts unchanged.

### Priority 1 implementation and verification — October 8, 2026

- Implemented `alerts/lifecycle.py` with stable operator controls, shared eligibility,
  serialized actions, manual-only threshold/archive actions, and action history.
- Applied only `075_alert_controls.sql` through `python -m alerts.migrate`. A database
  trigger transfers controls when source identity changes during journal promotion or
  legacy backfill. Original alert rows were not changed by migration.
- Added durable notification events, explicit delivery outcomes, bounded retries,
  interrupted-delivery review, per-alert action locks, and a single-worker poll lock.
- P7 exposes Pause/Snooze/Resume, delivery status, latest poll health, persistent output,
  and a read-only **Check Alerts — No Emails** button. Active overlays use current state.
  The searchable workspace and bulk management remain Priority 2 work.
- Backup: `fifth_dragon_capital_20261008_194007.dump`, created through
  `bash scripts/backup_db.sh --no-prune`; no old backups pruned. Verified the archive and
  restored it into a disposable database, which was removed after verification.
- Migration applied twice to the restored data: all 81 original alert records unchanged;
  20 legacy disabled/archived identities received durable suppression. Local post-migration
  inventory remains 81 records and 61 eligible alerts.
- Automated checks: 95 non-database tests and 17 isolated PostgreSQL/Streamlit checks.
  SMTP and quote fetching were mocked in tests; no real alert email was sent by tests.
- Local read-only Yahoo smoke check covered 61 alerts / 32 symbols. An existing invalid
  symbol returned no quote and was skipped without altering or deleting the alert.
- The five-minute launchd poller is installed and reads the current branch on each run.
  Its schedule was not changed; it remains live. The read-only button does not disable
  those background runs. Legacy continuously running pollers were not found at migration.

User acceptance still required:

- [ ] Restart Streamlit, open P7, and confirm Active/Inactive states and delivery columns.
- [ ] Pause a managed alert, run P10 **Brief only**, then verify it remains Paused in P7.
- [ ] Snooze an alert and verify its inactive state; resume with confirmation and verify
  it returns to Active. Resume may notify on the next scheduled poll if already satisfied.
- [ ] Run **Check Alerts — No Emails**, navigate/rerun, and verify output remains visible.
- [ ] Confirm charts/overlays still work; do not expect Priority 2 workspace features yet.

### Phase 1 — Baseline, backup, and acceptance fixtures

- [ ] Confirm branch, working-tree changes, migration numbering, test commands, and current
  compiler entry points. Preserve unrelated files such as `.idea/`.
- [ ] Repeat a read-only inventory by source and lifecycle state, including expirations,
  duplicate identities, missing source keys, and last-fired timestamps.
- [ ] Capture current P7 behavior and timings: first render, rerun, quote fetch, and poll.
  Do not include credentials or private account identifiers in committed fixtures.
- [ ] Verify whether the five-minute scheduled poller is actually installed/running;
  repository launchd definitions alone are not evidence of an active job.
- [ ] Before any production migration, create a fresh database backup and verify its
  contents with `pg_restore --list`; prove restoration into an isolated test database.
  Review `scripts/backup_db.sh` before reuse: it also prunes older backups. Prefer a
  one-off non-pruning backup for this change.
- [ ] Build fixtures for manual, structural, and journal alerts; expired, paused, snoozed,
  archived, triggered, missing-quote, duplicate, and source-deleted cases.

Gate: baseline recorded, backup/restore procedure verified, and existing tests pass.
No production data changes are authorized by the inventory step.

### Priority 1 — Dependable controls and polling

### Phase 2 — Lifecycle model and additive migration

- [ ] Design an additive schema for durable operator controls, including stable identity,
  pause/snooze state, timestamps, and action history. Keep existing alert IDs usable.
- [ ] Specify state transitions and precedence, including snooze expiration, manual resume,
  source disappearance/reappearance, journal TTL expiration, and source-key changes.
- [ ] Add constraints/indexes to prevent ambiguous override identities. Handle legacy rows
  lacking source keys deliberately; do not guess identity from a label alone.
- [ ] Preserve existing enabled/archive/trigger states during migration. Legacy disabled or
  archived rows must not become enabled as a side effect of backfill.
- [ ] Centralize lifecycle reads/writes in a service module; UI and compiler should not
  each implement separate SQL interpretations of Pause, Snooze, or Resume.
- [ ] Make migrations consistent with repository conventions; verify repeat execution is
  safe or explicitly recorded as already applied.

Checks: apply migration to a restored test database, compare row counts and existing
values, inspect constraints, and test every transition. Gate: no lost alerts, accidental
reactivation, or unrelated schema changes.

### Phase 3 — Source-aware compiler reconciliation

- [ ] Continue refreshing managed thresholds/labels from positions, watch levels, and
  journals while respecting durable operator suppression.
- [ ] Ensure repeated reconciliation is idempotent and does not reset pause/snooze or
  last-fired history. Remove blanket reactivation that conflicts with operator intent.
- [ ] Test stable source identity across row recreation. Keep suppression when a source
  temporarily disappears; document a deliberate reset policy when source identity changes.
- [ ] Keep manual alerts outside source-driven reconciliation.
- [ ] Route managed-level edits to their authoritative position/watch/journal workflow.
  Until safe navigation/editing exists, show the source and make the managed threshold
  read-only in the Alerts editor.
- [ ] Define archive semantics: archive manual alerts explicitly; for managed alerts,
  offer durable Pause or an explicit source-removal workflow. Do not imply that deleting
  an alert row permanently removes its source.
- [ ] Preserve expiration behavior and duplicate checks without using expiration cleanup
  as the only defense against an expired alert firing.

Checks: pause then run compiler twice; snooze then run brief/journal reconciliation; delete
and regenerate a managed row in a fixture; remove/reintroduce its source. Gate: source
updates remain correct and user lifecycle choices survive all supported refresh paths.

### Phase 4 — Safe polling, delivery, and observability

- [ ] Apply the shared eligibility predicate before quote fetching and again when claiming
  a notification, accounting for a user pausing an alert during a poll.
- [ ] Validate quotes and retain per-symbol fetch failures without marking alerts fired.
  Show unavailable/stale quote status with timestamps rather than fabricated prices.
- [ ] Add durable notification events with separate pending/sent/failed/not-configured
  outcomes. Preserve compatibility with existing trigger and last-fired history.
- [ ] Implement an atomic claim/lock strategy for concurrent polls and a bounded recovery
  policy for abandoned work. Keep network calls outside long-running database transactions.
- [ ] Specify bounded retry/backoff for failed delivery, event deduplication, and rearming
  after a condition becomes false. Do not spam continuously satisfied conditions.
- [ ] Document the SMTP limitation: crash recovery cannot guarantee exactly-once delivery
  after a server accepts mail but before the database records success.
- [ ] Add a dry-run diagnostic path that cannot send email or mutate trigger/delivery state.
- [ ] Persist poll start/end, eligibility counts, quote failures, notifications, delivery
  failures, and error summaries. Preserve manual-run output through Streamlit reruns.

Checks: mocked email success/failure/unconfigured cases, missing quotes, expired alerts,
pause-during-poll race, concurrent workers, failed-worker recovery, and repeated polls.
Gate: no expired/paused notifications, no duplicate claims, and delivery failure is visible
and recoverable rather than incorrectly reported as successful delivery.

### Priority 2 — Everyday management

### Phase 5 — Dedicated Alerts workspace

- [ ] Extract alert-management logic from P7 into shared services/components and add a
  dedicated Streamlit Alerts page. Select the next available page number at implementation
  time rather than renaming existing navigation unexpectedly.
- [ ] Provide three views: Needs attention, All alerts, and History. Needs attention includes
  satisfied conditions, nearby levels, delivery/quote failures, and review candidates.
  Separate expired, archived, and paused states in filters rather than a single bucket.
- [ ] Add ticker/label search and filters for source, lifecycle, condition, and trigger state.
- [ ] Show ticker, label, threshold, current quote/time, distance, source, lifecycle, expiry,
  and last event/delivery status. Define distance calculations for zero/invalid quotes.
- [ ] Use table row selection tied to stable IDs and a detail editor instead of a second
  long dropdown. Preserve selection safely across filtering and reruns.
- [ ] Support manual create/edit, Pause, Snooze, Resume, and explicit manual archive/restore.
  Validate ticker, finite positive threshold, condition, and expiry; warn about exact
  duplicates while allowing intentionally different levels.
- [ ] Replace ambiguous Clear Trigger with a clearly explained rearm/reset action, if
  retained. Warn that a currently satisfied condition can notify again on the next poll.
- [ ] Add confirmed bulk Pause/Snooze/Resume/Archive where permitted, showing counts,
  identities, source restrictions, and outcomes. Revalidate targets at execution time.
- [ ] Make permanent deletion exceptional and separately confirmed; prefer recoverable
  archive. Do not allow managed-row deletion to masquerade as source removal.
- [ ] Show latest poll health/output and quote timestamps; distinguish delivery failure
  from a healthy poll with no satisfied conditions.

Checks: filters/selection cannot affect hidden unintended rows; cancellation changes
nothing; successful operations persist after rerun and compiler refresh; failures retain
actionable output. Gate: common maintenance can be completed without scrolling giant
dropdowns, and each lifecycle state is understandable.

### Phase 6 — P7 integration and conservative hygiene tools

- [ ] Keep P7 focused on market charts and a compact alert summary/link to the workspace.
- [ ] Refresh alert overlays from current shared state during fragment refresh, not a
  stale closure captured during the initial full-page render.
- [ ] Exclude suppressed/expired/archived alerts from active overlays consistently; offer
  explicitly labeled historical overlays only if useful.
- [ ] Fetch/cache only needed quotes, reuse snapshots where practical, and load hygiene
  queries on demand. Measure against Phase 1 timings before claiming improvement.
- [ ] Keep exact duplicate consolidation preview-only until confirmed. Recheck eligibility
  and source priority at execution; preserve structural alerts and action history.
- [ ] Present aging manual alerts as review candidates, not automatic removals. Preserve
  the existing 90-day stale-manual threshold unless an explicit setting changes it.

Checks: periodic chart refresh reflects lifecycle changes without a browser reload;
duplicate consolidation cannot archive an unrelated or newly changed record; macro/futures
alerts are not incorrectly treated as obsolete. Gate: P7 charts remain functional and
alert management no longer dominates the page.

### Priority 3 — Opt-in brief-assisted maintenance

This work starts after Release A is accepted. It is not a prerequisite for fixing today's
alert-management problems. Symbol coverage or explicit trading levels may be absent from
a daily brief; in that case, generating no suggestion is the correct result.

### Phase 7 — Ownership and structured suggestion generation

- [ ] Add an ownership/maintenance setting separate from alert source and lifecycle:
  Manual / locked or Brief-assisted. Preserve the identity/source of existing manual
  alerts and default all of them to locked; do not infer opt-in from `pinned`.
- [ ] Explain that Brief-assisted means proposals only, never unattended price changes.
  Include per-alert opt-in/out in the detail editor. Do not expose Follow a source as a
  working mode until the separately approved deferred phase exists.
- [ ] Specify permissible evidence: an explicit level with symbol, level type/direction,
  evidence identifier, and as-of date. Generic market commentary, symbol mentions, and
  invented technical levels are insufficient. Preserve futures/index symbol identity.
- [ ] Identify whether the current brief inputs actually contain usable symbol-specific
  levels. Prefer deterministic matching of structured watch/journal levels; use the LLM
  only to interpret supported evidence and explain a proposed change.
- [ ] Extend the existing brief response schema with optional structured suggestions;
  preserve compatibility with the existing narrative output and usage reporting. Avoid
  an extra per-alert API call or an automatic second call when suggestions are absent.
- [ ] Review the external payload before implementation. The brief currently defaults to
  market-only input: do not silently enable portfolio sharing. Obtain explicit approval
  before sending private manual-alert levels, holdings, or journal content not already
  authorized for that workflow. If approval is absent, keep matching local and restrict
  model input to the existing permitted evidence.
- [ ] Validate suggestions locally: opted-in alert, exact normalized symbol, positive finite
  level, compatible condition/level type, resolvable evidence, fresh observation, and no
  duplicate/conflicting proposal. Use an explicit configurable freshness policy suited
  to source cadence rather than assuming every level expires in one trading session.
- [ ] Persist valid suggestions and provenance independently of brief Markdown, keyed to
  alert identity and brief run. Deduplicate repeated runs; retain review outcomes/history
  and supersede outdated proposals instead of building an endless queue.
- [ ] Isolate suggestion failure from brief generation and polling. Invalid output, missing
  evidence, or an unavailable LLM must never change active alert configuration.

Checks: locked alerts, symbol mention only, absent level, wrong direction, stale evidence,
symbol ambiguity, malformed response, repeat brief, conflicting levels, and API failure
all produce no unintended update. Gate: useful proposals can be generated with auditable
evidence, while every alert remains unchanged until explicit acceptance.

### Phase 8 — Review queue and controlled acceptance

- [ ] Add a Review suggestions view showing symbol, current/proposed level, condition,
  change size, reason, evidence/date, and the originating brief run.
- [ ] Provide Accept, Keep current level, and Snooze review actions. Snoozing a suggestion
  must not snooze the live price alert; label these distinct actions clearly.
- [ ] On acceptance, recheck ownership, evidence freshness, alert existence/version, and
  current threshold. Reject stale proposals if the user or a source has changed the
  alert since generation; do not overwrite newer edits.
- [ ] Apply acceptance through the local alert service in a transaction with a before/after
  audit record. Define/test rearm behavior for a changed threshold and warn if the current
  price already satisfies the proposed condition. Acceptance itself sends no email.
- [ ] Keep pause/snooze/archive state unchanged when accepting a threshold suggestion.
  Offer a deliberate undo that checks for intervening edits rather than blindly restoring
  an old value.
- [ ] For source-managed alerts, present a source-edit proposal rather than updating the
  compiled alert row. Do not silently mutate position stops/watch levels through this
  manual-alert assistance feature.
- [ ] Record rejected/snoozed suggestions so subsequent briefs do not repeatedly nag about
  the same unchanged proposal. A materially new level or newer evidence follows a clearly
  documented re-review policy.
- [ ] Initially support individual acceptance only. Consider bulk acceptance later, after
  the same exact-target previews and stale-version protections are proven reliable.

Checks: Accept/Keep/Snooze review, opt-out, rerun deduplication, intervening edit, concurrent
acceptance, expired evidence, paused alert, undo conflict, and currently satisfied price.
Gate: only the explicitly accepted current proposal changes a manual alert; no source,
lifecycle state, hidden alert, or email delivery is changed as a side effect.

### Shared release gates

### Phase 9 — Regression and browser acceptance

- [ ] Run the complete existing test suite plus new lifecycle/compiler/poller/UI-service
  tests. Test against an isolated database, not production notification recipients.
- [ ] Run migration/backfill twice as supported, then confirm original alert identities,
  source records, thresholds, and historical timestamps were preserved.
- [ ] Browser: create a manual alert, search/select it, edit it, pause/resume it, snooze it,
  archive/restore it, and verify persistence after navigation and reruns.
- [ ] Browser: pause a managed alert, run the Morning Brief/compiler, and verify it remains
  suppressed while its source-owned threshold can still refresh.
- [ ] Browser: verify journal TTL, source removal/reappearance, and expired snooze behavior.
- [ ] Browser: exercise bulk-action preview/cancel/confirm with mixed sources and confirm
  only the displayed eligible IDs changed.
- [ ] Browser: verify missing quotes, poll failure, and delivery failure remain visible
  after reruns; use dry-run/mocked delivery for development checks.
- [ ] Browser: verify P7 charts, overlays, auto-refresh, and sidebar navigation still work.
- [ ] Confirm P1/P10 sync and Morning Brief regression tests remain green; do not run a live
  E*TRADE sync merely as a documentation or unit-test verification step.
- [ ] Release B: exercise locked/assisted ownership, evidence inspection, Accept/Keep/Snooze
  review, stale-proposal rejection, repeated briefs, opt-out, and safe undo in the browser.
- [ ] Release B: validate LLM payload boundaries and added usage/cost; verify the existing
  brief still succeeds when suggestions are unavailable, invalid, or unsupported by data.

Gate: all automated tests pass, browser checks are signed off by the user, no unexplained
inventory differences remain, and measured UI performance has no material regression.

### Phase 10 — Controlled rollout and release

- [ ] Review the diff for unrelated edits, secrets, exports, and accidental financial-data
  fixtures. Update README with lifecycle meanings and operational troubleshooting.
- [ ] Record deployed commit, migration identifiers, backup location, and rollback steps.
- [ ] Coordinate a short poller maintenance window before production migration/deployment;
  do not run legacy and new workers simultaneously while their semantics differ.
- [ ] Apply the verified additive migration; deploy the compiler, poller, and UI together.
  Restart Streamlit as needed for newly imported modules and verify process versions.
- [ ] Run read-only/dry-run smoke checks before resuming notifications. Review the first
  live polling cycles and the next brief/journal reconciliation for unintended reactivation.
- [ ] Compare active/suppressed/expired counts and explain each change against confirmed
  actions or normal source updates. Never require the baseline counts to remain fixed.
- [ ] After user acceptance, commit/push and open a PR with test evidence, migration impact,
  notification policy, and rollback instructions. Merge only when release gates pass.

Rollback: stop the affected notification worker during investigation. Retain additive
schema and operator-control data; avoid destructive down-migrations. An older compiler or
poller may ignore suppression, so do not simply revert code and restart notifications.
Verify a compatible fallback or keep polling suspended until a forward fix is ready.
Use backup restoration only for demonstrated corruption, with explicit approval and an
assessment of newer production data that would be lost.

### Priority 4 — Deferred source-following automation

Requires a new design review and explicit implementation approval after Release B.

- [ ] Assess suggestion usefulness/rejection rates and whether automation would actually
  reduce maintenance. Do not convert assisted alerts automatically based on acceptance.
- [ ] Define Follow a source as an explicit link to one authoritative structured level,
  with a visible source identity and opt-out/detach behavior. Check existing compiler
  functionality before introducing a duplicate managed alert.
- [ ] Define bounds/review requirements for large changes, source disappearance, ambiguous
  replacement levels, or expired evidence. Retain the last valid value or pause for review
  according to the agreed policy; never infer replacement levels from prose mentions.
- [ ] Require audit history, reversible updates, preserved suppression state, race tests,
  and notification/rearm rules before a small opt-in rollout.

### Definition of done

- [ ] User Pause/Snooze survives all source reconciliation and managed-row recreation.
- [ ] Expired/archived/suppressed alerts cannot notify or appear as active overlays.
- [ ] Condition state and delivery outcome are separately observable and tested.
- [ ] Common alert management uses searchable, selectable rows and confirmed scoped actions.
- [ ] No automatic deletion, source-level mutation, or live notification occurred in tests.
- [ ] Automated tests, user browser acceptance, migration checks, and rollback review pass.
- [ ] The original June 2 plan remains preserved and clearly marked historical.

Release B additionally requires explicit assistance opt-in, validated evidence, a durable
review queue, guarded local acceptance, and no unattended alert/source updates. Deferred
source-following automation is not required to complete Releases A or B.
