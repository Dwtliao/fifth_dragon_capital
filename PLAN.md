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

### Current status and next actions — October 18, 2026

Canonical resumption notes: [docs/SESSION_HANDOFF.md](docs/SESSION_HANDOFF.md).
Watch Levels retirement Steps 1–5 are **complete and deployed**, including
migration 078. P10 retains position stops/notes; P11 is the discretionary alert
inventory with inline editing, no expiry by default and on-page notices. Email
is disabled. The poller was rechecked October 18 and is still unloaded.

- [x] Commit checkpoint `85f8e2e` and production retirement `368f514`.
- [x] Back up, test an isolated restored copy, apply the reviewed production
  migration and verify preserved data. Final isolated regression: 222 tests.
- [x] October 18: fetch origin; both implementation commits are now on the remote
  branch at `368f514`, with no divergence before this final update.
- [x] Save an explicit resumption handoff and update CLAUDE.md/current plan.
- [x] User accepted alert testing on October 18.
- [x] Correct P1's misleading default sync label from “90d” to incremental,
  documenting its watermark overlap and initial-history fallback.
- [ ] Commit and push the final handoff/label update and open a PR against main,
  as explicitly requested October 18. Leave the PR **unmerged** for review.
- [ ] Merge only after explicit direction. Keep email disabled and polling
  unloaded; do not restore snapshots, remove backups or modify stop policy.

The October 14 phased plan below is historical design context, superseded where
it conflicts with the implemented October 15 simplification. Do not rerun
Section 1, reintroduce saved Watch Levels, treat retired schema work as pending,
or automatically start Sections 2–6. Shared control/history/journal tables and
legacy alert source identities remain intentional. Counts in deployment notes
are October 15 observations, not an October 18 production inventory query.

### Historical handoff and phased plan — October 14, 2026

This section supersedes the October 13 next-task instructions below. That release
remains complete; do not redo it. The user requested an overall plan, with only
the first narrow changes in Section 1. Planning does not authorize implementing
all sections. Work ONE agreed section at a time and stop for user review.

**Principle:** `price_alerts` is the inventory of discretionary monitoring
instructions. Journals and saved watch levels supply ideas/reference information;
only explicit user action creates discretionary alerts. Holding stops are a
separate policy decision. No new queues or large state machine are needed.

Design input: `/Users/davidliao/financial_dashboard_alerts_architecture_recommendation.md`.
Its recommendations are proposals, not verified descriptions of every code path.

**Reboot handoff:** user reports committing and pushing prior changes. Verified
branch `dev/journal-review-refinements`, HEAD `bc58797` (`redesign plan Oct 14`),
with a clean working tree before this handoff edit. This new handoff edit is
uncommitted; preserve it. No Section 1 implementation has started. On resume,
read CLAUDE.md, this section, and the recovery guide; check git status. The next
agreed scope is Section 1 only: inspect callers, implement the narrow behavior,
run targeted isolated checks, then stop for user review. Do not redo the accepted
form or start Sections 2–6. No production cleanup/restoration, migration,
commit/push, PR/merge, or notification resumption without explicit direction.

**Current working state:** branch `dev/journal-review-refinements`; preserve all
local changes. The editable watch-idea manual alert form is implemented
and user-tested successfully; do not rebuild it. It allows symbol/direction/price/
expiry edits followed by preview/create, and leaves journal evidence unchanged.
Three isolated Streamlit form tests and 15 journal unit tests passed; this is not
a full release regression or authorization to commit/push/merge.

**Temporary production test:** alert/journal tables were backed up and cleared at
the user's request. A later Morning Pipeline run recreated 44 saved-level alerts.
The poller was unloaded and left paused; reboot/login may change scheduler state.
Verify its current status and keep notifications paused before further
operational work. See `docs/alert_journal_blank_slate_recovery_20261014.md` for the
persistent recovery schema, full dump, original row counts, and recovery procedure.
Original controls/purge blocks remain in the recovery snapshot. Do not restore,
clear, convert, or resume production state as a side effect of implementation.

#### Section 1 — Discretionary watch alerts become opt-in (first narrow change)

Scope: remove automatic discretionary creation/recreation and source ownership of
watch thresholds, while retaining the current tables and holding-stop behavior.

- [x] Inspect all creation/reconciliation callers: Morning Pipeline/Brief, P10
  Save All, source refresh, journal sync, and relevant CLI entry points. Classify
  position-stop identities separately from discretionary watch/journal identities;
  do not rely on source alone (both stops and watch levels use `key_levels_watch`).
- [x] Stop scheduled and user-triggered source reconciliation from automatically
  creating discretionary watch alerts from saved levels or old journal data.
  Journal ingestion continues staging ideas only. Saved levels remain editable.
- [x] Keep existing discretionary alerts and their thresholds intact. Source
  reconciliation must not overwrite explicit edits, reactivate suppressed alerts,
  or retire them merely because a reference level or holding changes. Preserve
  expiry and the existing notification/rearm behavior.
- [x] Preserve explicit manual creation and the user-accepted editable idea form.
  No new creation UI, preparation workflow, or journal extraction changes here.
- [x] Make removal of discretionary alerts an ordinary explicit Delete operation
  using the existing scoped preview/confirmation and audit snapshot behavior.
  Do not create new permanent recreation blocks for those deletions. Future
  journal ingestion may stage that symbol again; deleting an alert does not
  reject/blacklist journal ideas. Keep stop deletion/purge behavior unchanged.
- [x] Retain existing control/purge tables, triggers and historical blocks. Do not
  remove old intent or bulk-convert legacy alerts. Document any legacy managed-row
  editing restrictions for the later migration section rather than broadening
  this change. Do not silently bypass existing journal-approval purge guards.
- [x] Add targeted checks in an isolated database: saved levels and legacy journal
  sources cannot create discretionary alerts; deletion stays absent across repeat
  reconciliation; existing thresholds/control states survive source changes;
  explicit form creation uses the confirmed values; ingestion remains ideas-only;
  holding-stop maintenance and controls retain their prior behavior.
- [ ] User browser review: run the relevant source refresh with representative
  fixtures, delete a discretionary alert, repeat refresh, and confirm no return.
  Use mocked delivery for automated tests; do not invoke live extraction, trades,
  polling, or production cleanup for routine verification.

Gate: no surprise discretionary creation, source-driven overwrite, or recreation;
existing alerts retained unless explicitly removed; stop behavior unchanged.
Report changes/tests/limitations concisely and STOP. No schema consolidation,
production migration, backup restoration, commit/push, or release implied.

**October 15 implementation checkpoint — ready for user review:** source
reconciliation now maintains only exact `key_levels_watch` / `position:TICKER:stop`
identities. Watch/journal/manual rows, thresholds, lifecycle fields and controls
are retained unchanged by source refresh, including held/unheld transitions.
Legacy journal refresh/promotion/backfill entrypoints no longer write live alerts.
Delete retains scoped confirmation and audit/notification snapshots; only position
stops receive new purge blocks. Historical blocks and journal approval guards
remain intact. Legacy managed discretionary rows still cannot be edited directly;
that ownership migration remains Section 3. The accepted idea form is unchanged.

User additionally requested disabling noisy alert email: SMTP delivery is disabled
in code regardless of credentials. P11 shows fresh-quote condition notices on
explicit Refresh quotes, excludes suppressed/expired alerts, and does not monitor
when closed. The scheduled poller was found loaded after reboot and unloaded;
keep it paused. No production cleanup, restoration or schema changes were made.

Validation: 136 targeted tests passed, including 66 disposable PostgreSQL/Streamlit
checks; quote/delivery dependencies mocked, no emails sent. Python compilation and
`git diff --check` passed. This is targeted validation, not a full release run.
Original local handoff edits retained; no commit/push/merge. Sections 2–6 deferred.

Browser review remains pending: save changed watch reference levels in P10 and
verify existing discretionary alerts do not change; Delete a discretionary alert
in P11 with preview/confirmation, repeat P10 Save All, and verify it stays absent.
Check manual/idea-form creation and P11 Refresh quotes notices. Position-stop
maintenance remains active under the prior policy. Do not restore the recovery
snapshot or bulk-clean the recreated alerts as part of this review.

**October 15 follow-up — direct edits and no-expiry alerts (user requested):**
This supersedes the earlier UI deferral for discretionary edits only. P11 now
allows Label/Condition/Level edits directly in the main table, followed by Save
changes. Selection is reserved for bulk actions/history. All changed rows save
atomically with stale-target checks, validation, duplicate safeguards and audit
history. Legacy source/identity and controls remain intact; position-stop edits
are rejected and remain in P10. No ownership migration or source conversion runs.
New manual and explicitly approved journal alerts have no expiry by default.
Removed raw timestamp/day-expiry inputs from ordinary creation; the accepted
idea form otherwise retains its preview/create behavior. Existing expirations
are preserved unless explicitly removed via the table's Remove expiry checkbox.
Validation: 139 targeted tests passed using disposable PostgreSQL fixtures and
mocked quotes/delivery, including inline edits, rollback, stop protection,
no-expiry creation and source-refresh preservation. Browser acceptance pending.
Email remains disabled; do not resume background polling. No production writes,
restoration, migration, commit/push or merge performed by the agent.

#### October 15 — Watch Levels removal preparation (Steps 1–3 only)

User requested a checkpoint, dependency inventory/removal definition, and fresh
backup before attempting isolated removal testing or production changes. This
explicitly authorizes a local checkpoint commit; it does not authorize push,
merge, Watch Levels implementation, data deletion, restoration or migration.
The existing Section 1/direct-edit/no-expiry work is the checkpoint baseline.
The latest targeted run passed 139 tests with disposable PostgreSQL fixtures and
mocked quotes/delivery; browser acceptance remains separate from this checkpoint.

**Current database observations (read-only, October 15 at 17:54:23 CDT):**

| Object | Observed state | Removal boundary |
|---|---|---|
| `key_levels`, `section='watch'` | 27 rows; 25 support, 19 resistance, 16 alert-above values | Retire this saved-reference inventory after isolated testing and explicit production direction |
| `key_levels`, `section='positions'` | 23 rows, including 4 saved stops; no support/resistance/alert-above values | Preserve position records, stops, notes and identifiers |
| `price_alerts` | 34 legacy `watch:*` alerts, 4 exact position-stop identities, 1 manual alert | Preserve all existing monitoring rules; do not bulk-create alerts from retired reference rows |
| `alert_controls` | 1 legacy watch identity | Preserve user suppression and identity |
| `alert_purge_blocks` | No public rows currently | Keep shared stop protection and historical recovery snapshot; do not infer permission to drop shared machinery |
| `journal_review_items` | 16 watch-kind ideas/blocked items and 7 context items | Preserve journal suggestions/evidence/history; these are not the P10 saved Watch Levels list |
| `recovery_alert_journal_20261014_123231` | Present | Retain untouched |

There is **no dedicated Watch Levels table** in the public schema. `key_levels`
is shared with position stops. Journal, alert, control and history tables are
also shared features and must not be dropped merely because they contain the
word `watch`. The `key_levels_watch` source tag likewise identifies both stops
and legacy alerts. Retain those identities at the first removal boundary; any
later source-name conversion needs its own tested control/history migration.

The read-only catalog inspection found no foreign keys referencing `key_levels`,
no dependent database views, no user triggers on that table and no public stored
functions with textual `key_levels` references. Application dependencies remain
substantial. Current constraints include section in `positions`/`watch` and
unique `(section,ticker)`. Shared price-alert triggers preserve controls across
identity changes and prevent recreation of purged managed alerts; retain them.

**Application dependency map and proposed removal:**

| Location | Dependency / required future change |
|---|---|
| `dashboard/pages/P10_Morning_Brief.py` | Remove Watch Levels rows, add-watch form, delete widgets, watch-specific session keys and Save All payload. Keep position editing and stop reconciliation. Historic extraction display in Sync History is evidence, not a reference-data editor. |
| `morning_brief/fetchers.py` | Remove saved-watch quote enrichment. Make DB loading, saving, empty-table seeding and position sync use position metadata only. Generic saving currently reads/writes watch-specific columns and can delete/reinsert all rows; update callers before any column removal. |
| `morning_brief/key_levels.yml` | Remove the watch seed section, retain positions. Empty-table YAML seeding must never reintroduce retired saved references. |
| `morning_brief/brief.py`, `formatter.py` | Remove saved Watch Levels fetch/render path; preserve positions, market context, journal ideas and alert attention. Generated/archived historical briefs remain untouched. |
| `morning_brief/analysis.py` | Remove saved-watch evidence injection (currently used when portfolio context is explicitly enabled). Keep market and holding/stop evidence. Do not silently substitute automatic alert creation. |
| `morning_brief/alert_compiler.py` | Change key-level save/CAS/reload field lists and loops to position-only data. Preserve exact stop identities, locks, source reload and controls. Discretionary source creation is already disabled. Review obsolete watch helper code separately from explicit alert-management utilities. |
| `dashboard/pages/P11_Alerts.py`, `alerts/management.py`, lifecycle/attention/badge helpers | P11 remains the discretionary monitoring inventory and direct editor. Stop rows remain visible with editing in P10. No dependence on saved watch rows is required; retain existing legacy source identities and controls. |
| `morning_brief/journal_sync.py`, `journal_review.py`, `dashboard/journal_review_ui.py` | Preserve journal extraction/review and explicit Create Alert. The extraction field named `watch_levels` denotes suggestions, not a live saved-reference table; removing the P10 feature must not remove journal evidence or reactivate ingestion. |
| Schema definitions `066`, `069`, `070`, `075`, `076`, `077` | Keep historical migrations. Prepare a new, narrowly scoped migration only in a later authorized implementation stage; do not rerun old migrations against production. |
| Tests and documentation | Update P10 save/CAS/browser fixtures, saved-watch fetching/rendering/analysis tests and schema expectations; retain stop concurrency/control checks and journal/P11 tests. Update README/CLAUDE context so retired seed paths are not restored accidentally. |

**Proposed DB target for later implementation:** keep `key_levels` for position
metadata and stops; retire its watch rows and the watch-only `support`,
`resistance`, `alert_above` columns after every reader/writer is updated. Restrict
its section constraint to positions while preserving the existing IDs, stop/note
fields and unique key. No new replacement watch-reference table is needed.
Keep `price_alerts`, journal review/sync tables, alert controls, audit history,
notification history and all recovery tables. Do not automatically convert the
27 reference rows into alerts or delete the 34 existing legacy watch alerts.
These are definitions for review, not executable DDL or authorization to apply it.

**Fresh full-database backup:**

- File: `/Users/davidliao/Library/CloudStorage/Dropbox/Etrade/db_backups/fifth_dragon_capital_20261015_175416.dump`
- Created October 15, 17:54:16 CDT using `bash scripts/backup_db.sh --no-prune`.
- PostgreSQL custom format; 1,977,174 bytes.
- SHA-256: `ed4fcf34b138b2c2079c3f0e61b876092f07d3285d9f08c72b56055f953f9043`
- `pg_restore --list` succeeded. Verified table-data entries for key levels,
  alerts, controls, purge blocks, action/notification/poll history, journal
  review/evidence/decisions and sync log; October 14 recovery schema included.
- No prior backups pruned. This is archive-listing verification, **not a restore
  test**. Backup time and later read-only inventory time are distinct observations.
- Restore testing/removal work (Step 4) and production changes (Step 5) remain
  unstarted. Original recovery snapshot and October 14 backup remain intact.

Before any future production removal, take another backup if newer trading or
alert edits need protection. Do not restore the entire database over newer
production data; isolate any validation and review targeted recovery separately.

#### October 15 — Step 4 result: isolated Watch Levels removal tested

User authorized Step 4 only. Implemented and tested in a separate detached
checkout at `/private/tmp/fdc-watch-levels-step4`, based on `85f8e2e`; the running
workspace `/Users/davidliao/git_repos/fifth_dragon_capital` is unchanged, clean,
and still on `dev/journal-review-refinements`. No new commit, push or merge.
Do not confuse the isolated checkout's modified files with a production deploy.

**Implemented in the isolated checkout:**

- P10's second tab is Position Stops. Removed the saved Watch Levels grid,
  add/delete widgets and Save All watch payload. Added a direct Open Alerts
  button. Historical extracted journal ideas remain visible as evidence.
- Removed saved-watch quote fetching, brief table rendering, analysis evidence
  injection and YAML seed entries. Journal extraction/review and explicit P11
  creation are preserved. Existing P11 alert source identities are retained.
- Position-only loading and both save paths work before and after migration.
  DB-read failure is visible and disables P10 saving rather than treating it
  as an empty successful read. The brief still renders its market sections
  with a visible position-metadata-unavailable notice.
- Added `078_retire_saved_watch_levels.sql`: one bounded transaction removes
  only saved watch rows/columns and restricts `key_levels` to positions. Keeps
  position IDs/stops/notes/timestamps, all alerts/controls/history and recovery
  tables. No CASCADE. Unexpected position data or dependencies abort safely.

**Verification:** full backup `fifth_dragon_capital_20261015_175416.dump` restored
successfully into `/private/tmp` socket, port 55441, database
`fdc_watch_levels_step4`. No production .env was copied into the checkout.
Applied migration 078 twice. Removed 27 saved watch rows; preserved all 23
position rows exactly, including original IDs/stops/notes/timestamps. Hashed
all 50 other restored public/recovery tables before/after: row counts and
contents unchanged, including all 39 price alerts and original recovery data.
All sequence values unchanged. Dry-run stop reconciliation and read-only P10/P11
Streamlit smoke checks passed against the migrated restored database.

All **220 tests passed, none skipped**, using disposable schemas, dummy broker
credentials, isolated database URLs and mocked market/LLM/delivery dependencies.
Tests include double migration, preserved alerts/controls/sequences, rollback on
unexpected position data and view dependency, bounded lock contention, old-schema
reader/writer compatibility, stale-save and stop-approval concurrency, direct P11
editing, ideas-only ingestion, position-only seeding (even with legacy YAML watch
entries), and graceful brief metadata failure. `git diff --check` passed.
No broker sync/order, live journal extraction, real quote polling or email sent.

Detailed fingerprints/results: `docs/watch_levels_step4_verification_20261015.json`.
Repro helper: `scripts/verify_watch_levels_retirement.py`. It refuses checkouts
with `.env` and refuses any URL other than the designated local restored DB.
Requires a fresh pre-migration restore for its before/after comparison.
Regression log: `/private/tmp/watch_levels_step4_tests_final.log`.

**Step 5 remains unstarted.** Production schema/data and live code are unchanged;
email is disabled and background polling stays unloaded. For later review:

1. Review the isolated diff and preservation report; choose when to deploy.
2. Take a new backup if there have been intervening edits. Quiesce old P10/brief
   writers and deploy/restart the position-only code before applying migration
   078. New code supports the old schema, allowing read-only deployment checks
   first; old code requires retired columns and must not remain running.
3. Apply only the reviewed migration in its transaction, compare preserved data
   and verify P10/P11. No automatic alert conversion, cleanup or restoration.
4. A rollback after committed column removal is not a code-only revert to
   `85f8e2e`: old readers would fail. Prefer a forward fix with position-only
   code. Any schema/data restoration must be targeted, reviewed, backed up and
   explicitly authorized; never replace newer production trading data with the
   full test dump. Preserve recovery snapshots and sequence high-water marks.

The isolated database is stopped after verification and retained for review.
The temporary worktree contains uncommitted changes; keep it until the review
and any separately authorized promotion are complete.

#### October 15 — Step 5 complete: Watch Levels retired in production

User explicitly authorized production completion. Promoted the tested Step 4
files into `dev/journal-review-refinements` without overwriting local edits (the
main checkout was clean). Applied only reviewed migration 078; did not run the
broad production schema/bootstrap command, broker sync, live extraction or poll.

**Backup and transaction:** fresh full backup created at 18:03:44 CDT using
`bash scripts/backup_db.sh --no-prune`:
`/Users/davidliao/Library/CloudStorage/Dropbox/Etrade/db_backups/fifth_dragon_capital_20261015_180344.dump`.
Size 1,977,174 bytes; SHA-256
`f713ef03ab4aa634a90c8fd7df89fdd39c6fd9b9a75d19d416a3968cbbf68081`.
Archive listing verified, earlier backups retained. Migration committed at
18:04:57 CDT under the poller maintenance advisory lock and exclusive key_levels
lock with bounded timeouts. The exact tested SQL was executed within a wrapper
transaction so preservation comparisons completed **before commit**.

Removed exactly 27 `section='watch'` rows and the `support`, `resistance`,
`alert_above` columns; restricted key_levels to positions. Preserved all 23
position rows with original IDs/stops/notes/timestamps; all 39 alert rows; all
50 other public/recovery tables byte-equivalent by sorted row fingerprints;
all sequence values; and both shared alert-control/purge triggers. No alert
conversion, automatic creation, historical purge clearing or recovery restore.
Post-commit read-only comparisons confirmed the same preserved contents.

**Final deployment checks:** 222 tests passed, none skipped, in the separate
checkout against disposable PostgreSQL schemas with mocked external dependencies.
Read-only production Streamlit AppTest checks passed for P10 and P11; live Chrome
checks confirmed Position Stops and all 39 P11 alerts. Temporary Streamlit health
endpoint returned `ok`. No UI save/create/delete/poll action was performed.
The user chose to start Streamlit manually; the agent's temporary verification
instance was stopped. The disposable test database was also stopped and retained.

Two narrow integration fixes were included and tested before deployment:

- Ordinary schema initialization and broad migrate no longer replay 069's
  expiry data backfill when the expiry column is already installed. This
  prevents future normal sync from assigning expiry to an explicit no-expiry
  alert with a blank label; additive DDL repair remains available.
- Display of a previously generated brief hides only its legacy KEY LEVELS
  table, preserving the stored file and other sections. Fresh briefs no longer
  generate it. Future attention instructions point to P11 and P10 stops.

Evidence: `docs/watch_levels_step5_deployment_20261015.json` contains the fresh
backup manifest, migration SHA, preserved-table hashes and final check results.
`git diff --check` passed. These deployed changes are saved in the local commit
containing this note. No push or merge; original checkpoint `85f8e2e` retained.
Email remains disabled and `com.fifthdragon.alert-poller` is confirmed unloaded.

Start manually from the main checkout with the existing venv's
`streamlit run dashboard/app.py`. Review P10 Position Stops and P11 Alerts.
The retired watch references are only in backups/recovery history, not active
reference tables. The shared `key_levels` table remains for stops/notes.
Old `key_levels_watch` alert identities are intentionally retained to preserve
controls/history; they no longer imply source ownership of discretionary levels.

Rollback constraints from Step 4 still apply: do not run old readers against the
retired schema, rewind sequences, or overwrite newer production data with a full
dump. Prefer a forward fix, keep notifications paused, and review any targeted
restoration separately. Retain both backups, the recovery schema and isolated
worktree until the user accepts the result.

#### Section 2 — Decide holding-stop monitoring policy (deferred)

- [ ] Choose universal opt-in or explicitly enabled automatic stop monitoring.
  Recommendation for discussion: explicitly enabled automatic monitoring, clearly
  distinct from discretionary watches. No policy is selected by this plan.
- [ ] Define enabling/disabling scope, saved-stop changes, position closure and
  reopening, stale/unavailable holdings, and the effect of deleting a managed stop.
- [ ] Define a visible monitoring setting so deletion does not silently recreate
  protection or silently turn it off. A notification is not a broker stop order.
- [ ] Implement only after the decision, with isolated transition tests and browser
  acceptance. Preserve existing behavior until this section is agreed.

#### Section 3 — Reconcile legacy alerts and retire obsolete purge logic (deferred)

- [ ] Inventory legacy watch alerts, identities, controls, purge blocks, and review
  links, including the temporary-test recovery snapshot. Resolve restore/retain
  choices with the user; do not assume the current test inventory is the baseline.
- [ ] Plan explicit ownership conversion for retained discretionary managed rows
  so normal manual editing is available without losing IDs, controls or history.
- [ ] Verify no old caller/worker can recreate watches; then retire obsolete watch
  purge checks/blocks without restoring previously unwanted alerts. Keep any
  agreed holding-stop policy distinct. New explicit intent must be usable again.
- [ ] Back up before approved data/schema changes; test migration repeatability,
  stale/concurrent operations, preserved controls, and rollback in isolation.
  Do not drop a table/trigger merely because its watch use became obsolete.

#### Section 4 — Simplify everyday management and creation (deferred)

- [ ] Review pause/snooze/archive/delete meanings with concrete user flows. Consider
  folding controls into alert rows only after identity-preservation needs are gone.
  Do not collapse enabled/triggered/snoozed/delivery into one ambiguous status.
- [ ] Offer Create Alert from saved reference levels using the accepted editable
  form; prefill the actual instrument, direction and price and require confirmation.
- [ ] Keep the journal/manual entry workflow consistent and avoid additional
  required review stages. This section does not redesign polling semantics.

#### Section 5 — Journal idea recurrence and dismissal (deferred)

- [ ] Inspect current deduplication and review-state retention before changing it.
  Define dismiss-for-now versus incorrect extraction and what new evidence should
  resurface; avoid permanent symbol blacklists and repeated unchanged suggestions.
- [ ] Preserve evidence and decisions when the user edits a manual rule. Decide
  whether a dedicated creation link is useful beyond today's idea number in label.
- [ ] Test repeated, newer and materially changed evidence, dismissed ideas, deleted
  alerts, and explicit recreation. Ingestion never activates notifications.

#### Section 6 — Notification retention and release checks (deferred)

- [ ] Decide retention/reporting needs before changing event deletion behavior.
  Current purge already snapshots events into action history, but original event
  rows cascade-delete. Compare retaining parent rows versus nullable event links.
- [ ] Document existing first-observation, threshold satisfaction, rearm and retry
  behavior; preserve it unless a separate behavior change is explicitly selected.
  Verify repeated polls and quote gaps without sending real notifications.
- [ ] At a release checkpoint, run full regression and required isolated database/
  browser checks. Review migrations, deployment compatibility and rollback.
- [ ] Obtain explicit direction for production migration/restoration, commit/push,
  PR/merge and resuming notifications. Resuming the launchd agent polls immediately.
  Retain recovery backups until the user accepts the restored/final state.

### Historical handoff — October 13, 2026

**Release is complete, not WIP.** PR #87 merged the journal approval feature on October 12.
Current main: `72497c6`; release commit: `68f1108`. Earlier PR #85 shipped alert management
and PR #86 shipped hygiene/brief attention. Old unchecked items and WIP warnings below
are historical unless explicitly listed as remaining work here. Do not redo completed work.

- Journal sync stages ideas only; new alerts/stop changes require P10 Journal Review
  approval. Mentions are context, ordinary technical levels are watch ideas. Preparation,
  rejection and acceptance are distinct; rejection never deletes an existing live alert.
- Approved watch alerts are operator-owned/manual with a 21-day default. Holding stops
  require verified holdings within four calendar days. Simple above/below conditions only;
  unsupported timeframe/range language needs explicit clarification. Existing controls
  and purge intent are preserved. Approval can lead to email on the next scheduled poll.
- P10 has Sync Latest Journal and Sync Last 5 Journals. Never restore Sync All: selection
  is at most five most recently modified FILES, not necessarily five days; unchanged files
  skip. CLI defaults to five, supports --recent 1–5/--file and bounded --force, no --all.
- Real journal sync initially failed on string commentary; now malformed optional
  commentary warns/skips while valid ideas survive. Actionable arrays remain strictly
  validated before DB access. User confirmed the corrected real journal sync worked.
- User tested Journal Review, quote refresh/already-met warnings and Sync Positions.
  Sync Positions affects saved metadata, not trades. Result persists across reruns, explains
  added/removed symbols and distinguishes rejection from levels-saved/refresh-failed.
- Groups A/B/C fixes are shipped: row-aware source CAS including notes, targeted writes,
  preserved unseen rows, dry-run rollback, ordered lock acquisition/released-lock retries
  carrying new requirements, protected source reload, pinned widget baselines and explicit
  refresh, truthful partial-completion reporting. Do not restart the earlier review loops.
- Release verification: **211/211 tests passed**, including isolated PostgreSQL/Streamlit
  tests; migrations 075–077 repeated with original alerts/levels unchanged. Read-only
  verification confirmed live review schema 077 present. No additional migration needed
  merely for a new session. Do not run migrations, live extraction, notifications or trades
  as routine diagnostics. Scheduled polling remains live independently of the agent.

**Remaining small refinements — ask the user which ONE to address:**

1. Parse older filename dates such as `trading_journal_august13_2026.md`; current extraction
   recognizes ISO filename dates only, so older proposals show unknown evidence date.
2. Explain redundant holding-stop proposals more clearly (e.g. URNM below 49.85, existing
   alert #5). A quoted journal phrase "active alert unchanged" is historical evidence, not
   a live-status guarantee. Do not automatically accept, delete or modify those alerts.
3. Archive statistics include already-archived rows; banner honestly says "marked or kept
   archived". A later isolated change may count actual transitions instead.
4. A different proposal matching a purged condition can still appear as review clutter,
   although approval is blocked. Presentation/filter refinement only after user selection.

**Working preference:** ONE agreed section at a time, smallest scoped change, targeted
tests first, concise changes/tests/risks report, then STOP for user review. No unrelated
refactors, speculative improvements, long repetitive writeups or repeated full regressions.
Use full regression at release checkpoints. Ask before expanding scope. Commit/push,
PR/merge and live migrations require explicit direction. CLI mode toggle is Shift+Tab.

**New-session entry:** read CLAUDE.md and this section, check git status, preserve local
changes, then ask which refinement the user wants. Before implementation create a new
feature/fix branch from updated main; never commit directly to main. There is currently
no authorized next coding task. This handoff edit itself is local/uncommitted until saved.

**Testing:** `/Users/davidliao/git_repos/py312/venv/bin/python -m unittest discover -s tests -q`
runs default tests (DB cases skip without ALERT_TEST_DATABASE_URL). Opt-in DB tests require
a disposable database, NEVER production. The local `/private/tmp/verify_alert_task1.py`
full-suite helper restores a backup, tests migrations, then removes only its scratch DB;
it is not tracked and may not exist on another machine. Inspect it before use. See backup
and test details in the older handoff below; do not restore that backup into production.

### October 11–12, 2026 — Journal-to-alert approval (completed release history)

**Release checkpoint — October 12, 2026:** User reports Journal Review working in
Streamlit and accepts this initial version for commit/push/PR merge, with future refinements
kept separate. Manual checks covered real journal staging after parser correction, bounded
journal selection, review quotes/already-met warnings, and Sync Positions result visibility.
Final full regression: **211 tests passed, no skips**, using the restored isolated backup;
migrations 075–077 applied twice with original alerts/levels unchanged. The previously
failing sidebar browser test lacked a holdings-view fixture; mocked the holdings read so
both rejection and partial-completion paths actually execute. No production code change
was needed for that test correction. `git diff --check` passed. Read-only inspection
confirmed migration 077's review table exists in the live DB; no live migration, extraction,
approval, SMTP or trade was run by the release verification. Earlier WIP warnings/checklists
below are historical checkpoints, superseded by the Group A/B/C fixes and this acceptance.
Remaining refinements: parse spelled-out journal dates; clearer redundant-stop presentation;
archive counters should eventually count transitions rather than already-archived rows.
Do not expand these during release. Preserve one-section-at-a-time review for future work.

October 12 user requested no Sync All operation. Replaced it with Sync Last 5 Journals,
selected by modification time (same policy as latest journal), skipping unchanged files.
CLI defaults to a maximum of five, offers --recent 1–5/--file and bounded --force, and
removes unbounded --all. Weekly journals mean five files, not five days. 15 journal-review
unit tests and 8 pipeline/UI tests passed; no real extraction, migration, commit or push.

October 12 user-tested Journal Sync successfully after the commentary parser fix; Sync
Positions result also survived its rerun. User requested clearer success wording. Updated
only its banner: explicitly describes metadata, added/removed symbols, managed alert
changes, historical archive-count inclusion, retained stops/notes and no trades. Underlying
sync/reconciliation behavior and error/partial-completion paths unchanged. Eight targeted
pipeline/UI/message tests passed; no broad full-suite rerun or migration for this change.

October 12 targeted runtime fix (primary agent takeover): actual journal sync failed
because optional commentary contained strings and normalization assumed objects.
Added explicit commentary object examples to the prompt; malformed optional commentary
is skipped with a visible warning, retaining valid actionable ideas. Required actionable
arrays are strictly checked for object entries before staging/DB access. Added four
regression cases (string/mixed commentary, invalid container, malformed actionable items).
Verification: 13 journal-review unit tests + 6 Claude integration tests passed;
`git diff --check` passed. No full-suite rerun, migration, real LLM retry, live-data writes,
commit or push by this fix. Other agent's uncommitted work is preserved. Stop for user
review; next manual check is Sync Latest Journal only, not a full pipeline rerun.

Development branch: `feature/journal-alert-approval`, from updated main after PR #86.

Approved decisions: review lives in P10; journal-proposed holding stops and changes also
require approval; newly approved watch alerts default to 21 days, editable before acceptance.

**Working agreement — user preference, October 11:** Complete only ONE section at a time,
report what changed/tests/limitations, and STOP for user review before starting the next
section. Do not implement multiple sections in one turn, apply live migrations, invoke
live journal extraction, or proceed to rollout without that section's review. Keep work
small to respect the user's weekly usage budget. The initial implementation below moved
through multiple sections without the requested checkpoints; it is local WIP, not accepted.

Execution order and checks:

1. [x] Create/publish the development branch and preserve this plan. Codex CLI mode toggle
   in the user's session is Shift+Tab. Do not commit directly to main.
2. [x] Backup and migration PREPARATION: non-pruning backup completed; additive migration
   077 written and tested twice on a restored isolated database. Durable review items,
   evidence and decision audit added. Live migration is NOT applied; rollout is Step 10.
3. [x] Implemented locally: separate journal extraction from activation. Commentary is context; explicit levels
   become watch ideas; notification instructions become pending candidates; holding stops
   become pending proposals. Neither live key levels nor price alerts may change on sync.
4. [x] Implemented locally: preserve exact evidence/source/date, validate symbols/finite levels, and distinguish
   intraday from daily/weekly-close or sustained conditions. Ambiguity must require explicit
   clarification, not silently become an intraday alert. Keep the existing single LLM call.
5. [x] Deduplicate by trading condition, not wording/file/run time. Repeat accepted/rejected
   ideas do not create alerts, renew expiry, or nag. Material level changes require review.
   Disable automatic promotion of recurring journal ideas into permanent alerts.
   Implemented: exact condition fingerprints, durable accepted/rejected decisions, evidence
   history, no expiry renewal on repeats, no automatic recurrence promotion. October 11
   follow-up: `review_action` now blocks approving a purged WATCH/JOURNAL (non-stop) alert
   back into existence (new/replace modes), mirroring the existing managed-STOP purge
   guard — same `alert_purge_blocks` table, exact ticker/condition/threshold match. Staging
   (`stage_extraction`) was already confirmed to re-use the durable `accepted` review-item
   row on resync rather than re-entering the queue, so no staging-layer change was needed;
   see `test_purged_watch_alert_cannot_be_silently_restored` and
   `test_resync_after_purge_does_not_reenter_queue` in `tests/test_journal_review_db.py`.
   Caveat (user-noted October 11): a *different* proposal (different idea_key — e.g. a
   materially different timeframe/wording) matching the same purged condition can still
   enter the review queue as a new pending/idea row; only its approval into a live alert
   is blocked. This stops notification recreation but does not eliminate resurfacing
   clutter in the queue; a staging-level suppression was intentionally not built this
   round — revisit if the clutter becomes a practical problem.
6. [x] Implemented locally: P10 Journal Review: separate ideas/candidates, group by ticker, unchecked selections,
   show evidence/date/existing alerts/holdings, preview selected approve/reject actions.
   Ideas require deliberate preparation before they become notification candidates.
7. [ ] Approval creates operator-owned manual watch alerts (21-day default) or updates only
   the explicitly selected manual alert/held-position stop. Recheck versions, holdings,
   targets/controls and duplicates under shared locks; batches are all-or-nothing. Preserve
   pause/snooze, reject purged managed restoration, audit before/after; never send email.
   Core approval service and tests implemented. User browser acceptance remains; not
   release-complete.

   **October 11 concurrency review (source compiler vs. P10 approval):**
   - Reviewed `review_action` (journal_review.py), `apply_bulk` (alerts/management.py),
     and `reconcile_alerts`/`_apply_upsert` (alert_compiler.py) for shared lock ordering,
     stale previews, stop-source/alert consistency, and all-or-nothing batches.
   - Approval and purge/bulk-controls already share `LOCK_NAMESPACE` conventions correctly
     (per-target-id lock, per-ticker symbol lock, stop/purge guard locks), sorted
     consistently to avoid deadlock, with fingerprint/revision/old-value rechecks after
     locking — no gap found there; existing tests (`test_stale_preview_batch_rolls_back...`,
     `test_replace_preserves_pause_and_rejects_changed_target`, purge tests) already cover
     this.
   - **Gap 1 found and fixed — no lock at all.** `alert_compiler.reconcile_alerts` (used by
     `reconcile_structural_alerts` for key_levels_watch/stop rows) took no advisory lock
     before reading and writing managed alert rows, despite being reachable from the
     morning pipeline/P10 "Sync Positions" concurrently with a user mid-approval. Fix:
     acquire the same per-ticker `LOCK_NAMESPACE+1` symbol lock manual P11 edits and
     journal-review approvals already use, for every ticker it will touch, before loading
     current rows. Demonstrated the gap (unfixed code let the compiler proceed
     unsynchronized) and the fix (it now blocks) by temporarily reverting and re-running;
     see `test_compiler_reconcile_blocks_on_concurrent_stop_approval_lock`.
   - **Gap 2 found and fixed — the lock alone doesn't stop a stale read ($90→$95).**
     Caught on review: the per-ticker lock only keeps two writers from stepping on each
     other *while they write*; it does nothing about a reconcile that already read
     key_levels (e.g. stop=$90) from the DB *before* a concurrent approval moved it to
     $95. Once that reconcile finally gets the lock — after the approval has committed —
     it would still write its stale $90 back over the just-approved $95: the lock closes
     the window but the snapshot taken before the window opened is still wrong. Fixed by
     giving `reconcile_alerts` an optional `reload_desired` hook, called only *after*
     locks are held, that re-reads the authoritative source and replaces the desired set
     before any diffing/writing. `reconcile_structural_alerts` takes a parallel
     `reload_key_levels` and `refresh_structural_alerts_from_db` (the actual DB-reload
     entrypoint used by the morning pipeline) wires it to `load_key_levels_from_db`.
     `reconcile_structural_alerts`'s other caller (P10, which passes an in-memory dict
     it just saved) is unaffected — no reload hook, same behavior as before.
     Reproduced the exact $90→$95 scenario and proved both the saved stop and the live
     alert land on $95, not $90, confirmed to fail without the fix and pass with it; see
     `test_compiler_reload_prevents_stale_stop_overwrite`.
   - **Gap 3 found and fixed — lock-order inversion (deadlock risk).** Caught on review:
     a stop approval acquires its purge-guard lock (`LOCK_NAMESPACE+2`) *before* the
     per-ticker symbol lock (`LOCK_NAMESPACE+1`); the compiler's insert path acquired the
     symbol lock in Python first, then hit the *same* guard lock implicitly inside the
     `prevent_purged_alert_recreation` DB trigger on `INSERT` — i.e. symbol-then-guard,
     the reverse order. Two sessions taking a shared pair of locks in opposite orders is
     a textbook deadlock (compiler holds symbol, waits on guard; approval holds guard,
     waits on symbol) — Postgres's deadlock detector would eventually abort one side with
     an error rather than hang forever, but that's still a startling, unnecessary
     approval/reconcile failure. Fixed by having `reconcile_alerts` acquire the identical
     guard-lock keys (`"source:ticker:condition"`, hashed via `hashtext` exactly like
     `apply_bulk`'s purge guard and the DB trigger) *before* the per-ticker symbol lock —
     matching `review_action`'s order everywhere a guard lock is involved. Verified the
     compiler now queues on that exact guard key (same namespace, same hash input) before
     doing anything else; see
     `test_compiler_acquires_same_guard_lock_as_stop_approval_in_matching_order`. True
     deadlock-freedom follows from both paths now using one consistent lock order
     (standard argument, not something a timing-based test can prove by itself — the test
     confirms the ordering and key identity the argument depends on).
   - **Limitation / not fixed this round:** `apply_bulk`'s purge path does not take the
     `LOCK_NAMESPACE+1` ticker lock (only its own guard + target-id lock), so a purge
     racing a concurrent compiler reconcile for the same ticker isn't covered by gap 1's
     fix. Not treated as a demonstrated gap: `_apply_upsert`'s plain `UPDATE ... WHERE
     id=%s` against a row purge deleted mid-transaction simply affects zero rows (no
     crash, no data resurrection, no incorrect overwrite) — it would only misreport that
     row as "updated" in the compiler's stats. Flagging for a future round rather than
     fixing speculatively.
   - **Unchanged from Step 5:** purged conditions can still reappear in the review queue
     as a *new, different* proposal (different idea_key); only approving it back into a
     live alert is blocked. This is a UI-clutter limitation, not a data-safety one.
   - Full isolated-DB regression (`/private/tmp/verify_alert_task1.py`, reviewed for safety
     before use — only ever touches a disposable scratch database restored from the real
     backup, applies migrations there, and drops only that scratch database afterward):
     **191/191 tests passed** (188 prior + 3 new), restored backup's 92 alerts/50 controls
     unchanged after migrations re-applied twice. Production database untouched throughout.
     (One intermediate verification run was killed mid-hang after a test assertion failed
     before its cleanup ran, which left one lock-holding connection open and one scratch
     verification database undropped; both were found and removed before the final clean
     run — production and the real backup were never touched by this.)

   **October 11 (second pass) — committed as WIP, NOT yet signed off.** This round's
   fixes (reload-staleness, lock ordering, reload lock-coverage retry, P10 CAS save) were
   committed and pushed as `9419f34` "WIP Step 7" on `feature/journal-alert-approval`
   (193/193 tests passing at commit time). On further review, **four blockers remain in
   that committed code** — the 193 passing tests do not cover any of them, and
   revert-verification (confirming a fix fails when reverted) does not substitute for a
   test asserting the actual final-state invariant under each scenario:

   1. **Retry-loop lock inversion.** `reconcile_alerts`'s fixed-point retry (added to
      close the "reload introduces an unlocked ticker" gap) acquires new guard locks on
      iteration 2+ while *still holding* iteration 1's symbol locks — i.e. across
      iterations it can do guard_A, symbol_A, guard_B (while holding symbol_A), symbol_B.
      That's the identical guard-vs-symbol inversion the single-pass fix was built to
      eliminate, reintroduced by the retry itself. Five bounded attempts don't prevent a
      deadlock *during* acquisition; they just bound how long it can take to get into one.
   2. **P10 stale widgets not actually covered.** `baseline_kl` does reload correctly on
      every Streamlit rerun, but Streamlit's `number_input(value=..., key=...)` only uses
      `value` to initialize a widget the first time its key appears in `session_state` —
      on later reruns the widget keeps its last in-session value regardless of what the
      DB now holds. So the realistic failure mode (page opened once, left open, an
      approval lands, user clicks Save All without a browser refresh) is not exercised by
      `test_p10_save_rejects_stale_baseline_instead_of_clobbering_approval`, which only
      calls the backend helper directly with synthetic stale dicts — it proves the CAS
      check works *given* a stale baseline, not that P10 actually produces/catches one in
      the real widget-rerun sequence.
   3. **CAS comparison is incomplete, but the save is a full-table replace.** `_flat()`
      only compares `stop/support/resistance/alert_above`; `note` is never compared, so a
      concurrent note-only change is silently lost. Worse: `touched` is only
      `baseline ∪ (this call's new key_levels)` — a ticker/row added by a third party
      that is in neither set (not edited by this call, not yet known to this baseline) is
      never checked for conflict, yet `save_structural_key_levels` still does
      `DELETE FROM key_levels` (whole table) and reinserts only from the passed-in
      `key_levels` — silently deleting that untouched, concurrently-added row. The CAS
      check's coverage and the save's blast radius don't match.
   4. **`dry_run` doesn't dry-run the key_levels save.** `save_structural_key_levels(...,
      dry_run=True)` still executes `DELETE FROM key_levels` + re-`INSERT` and
      unconditionally `conn.commit()`s before ever looking at `dry_run` — only the
      downstream `reconcile_structural_alerts(..., dry_run=dry_run, ...)` call respects
      it. A caller passing `dry_run=True` expecting a no-op preview gets a real write.

   **Also noted, not yet a numbered blocker:** both P10 call sites treat
   `_save_levels_and_refresh_alerts`'s return dict as success regardless of whether it
   contains an `"error"` key (the shape used when the helper raised) — i.e. a CAS
   conflict or any other failure is reported to the user as
   `st.sidebar.success(...)`/`st.success(...)` with "created=0 updated=0 ...", not as an
   error. The conflict is swallowed exactly when it matters most (the save didn't
   happen).

   **Next checkpoint plan:** grouped the four blockers by actual coupling rather than
   fixing in numbered order — #3 and #4 share one function and one write path, so fixing
   them separately would mean rewriting that function twice; #2 and the misleading-UI
   note are both Streamlit/P10-layer and need `AppTest`-level coverage together; #1 is
   independent (different function, no shared state). Order: **Group A (#3 CAS
   completeness + #4 dry_run) → Group B (#1 retry-loop lock ordering) → Group C (#2 P10
   stale widgets + misleading success/error UI)**, stopping for review after each group.

   **Group A — fixed.** `save_structural_key_levels` rewritten:
   - Comparison/identity is now keyed by `(section, ticker)` (matching the table's actual
     `UNIQUE (section, ticker)` constraint) and covers all five fields including `note`,
     not just the four numeric ones.
   - The write path no longer does `DELETE FROM key_levels` (whole table) + blind
     reinsert. It now only deletes rows present in `baseline_key_levels` but absent from
     the new `key_levels` (the user's explicit removals) and upserts
     (`ON CONFLICT (section,ticker) DO UPDATE`) only rows in the new `key_levels` — a row
     in neither (added by another session after baseline, concurrently with this call) is
     never touched.
   - `dry_run=True` now actually rolls back instead of unconditionally committing; only
     then is `dry_run` forwarded to the downstream `reconcile_structural_alerts` call.
   - New tests, each a final-state assertion (not just revert-verification):
     `test_p10_save_rejects_note_only_conflict`,
     `test_p10_save_preserves_row_added_by_another_session`,
     `test_p10_save_removes_row_explicitly_dropped_by_user`,
     `test_p10_save_dry_run_writes_nothing`. Confirmed `test_p10_save_dry_run_writes_nothing`
     fails without the fix (reverted the `dry_run` gate, got
     `AssertionError: Decimal('95') != 90`, restored the fix, reran clean) — did not repeat
     that revert-and-confirm step for the other three new tests this round, given time/
     context constraints; they are final-state assertions against the rewritten code,
     not yet independently proven to fail against the old code.
   - Full isolated-DB regression: **197/197 tests passed** (193 prior + 4 new), restored
     backup's 92 alerts/50 controls unchanged, scratch DB dropped after. Production
     untouched throughout.

   **Group B — fixed.** `reconcile_alerts`'s reload-coverage retry previously held
   phase-1's symbol locks across iterations while acquiring additional guard locks for
   a newly-discovered ticker — reintroducing, inside the retry loop itself, the exact
   guard-vs-symbol inversion the ordering fix had just eliminated. Restructured into an
   outer bounded (5) attempt loop, each attempt on a *fresh connection*:
   - Phase 1 (bounded, reload-driven): acquire/stabilize guard locks only, never
     touching the symbol namespace.
   - Phase 2: once guards are stable, acquire ALL symbol locks for the final ticker set
     in one batch.
   - One more reload+coverage check after Phase 2: if it reveals a requirement
     (guard or ticker) this attempt never locked, the attempt is abandoned — raises an
     internal `_LockCoverageDrifted`, caught to `conn.rollback()`/`conn.close()` (releasing
     *everything*, including the Phase-2 symbol locks) — and retried from zero on a new
     connection, rather than patched by acquiring the missing guard in place.
   - Net effect: within any single attempt, and across attempts, a guard lock is never
     acquired after a symbol lock already was — closing the loophole without weakening
     the reload-coverage guarantee from the prior round.
   - New deterministic test,
     `test_compiler_releases_symbol_lock_before_expanding_guard_coverage`: drives a
     reload stub that introduces a second ticker only *after* Phase 2 has acquired the
     first ticker's symbol lock, forcing exactly one abandon-and-retry cycle. Uses a
     second real connection's non-blocking `pg_try_advisory_lock` as a probe (no threads,
     no timing guesses) to assert, synchronously, that the symbol lock is held at the
     moment of attempt 1's coverage check and released by the time attempt 2's first
     reload runs. Confirmed this test fails against the pre-fix interleaved-retry code
     (reverted to it, got the expected failure, restored the fix, reran clean).
   - Full isolated-DB regression: **198/198 tests passed** (197 prior + 1 new), restored
     backup's 92 alerts/50 controls unchanged, scratch DB dropped after. Production
     untouched throughout.

   **Group B follow-up — one more ordering risk found and fixed.** Caught on review:
   Phase 1 retained already-held guard locks while acquiring newly-discovered ones from a
   reload, without checking whether the new guard sorted *before* an already-held one
   (e.g. holding guard "...Z..." from pass 1, then a reload reveals guard "...A..." is
   also needed — "A" < "Z" alphabetically). Acquiring A at that point is still
   guard-before-symbol locally, but it breaks the *global* ascending order every other
   session relies on: another session legitimately holding A and waiting on Z would then
   deadlock against this one (holds Z, waits on A). Fixed:
   - Phase 1 now checks, before acquiring any newly-discovered guards, whether the
     smallest new one sorts before the largest already-held one; if so, this attempt is
     abandoned (same `_LockCoverageDrifted` abandon-and-retry-on-a-fresh-connection
     mechanism as the earlier fix) rather than acquiring it in place.
   - `_LockCoverageDrifted` now carries the `desired_map` known at the moment of
     abandonment, and the outer attempt loop seeds the *next* attempt from it (not from
     the original `initial_desired_map`) — so a retry starts already knowing about
     whatever was discovered, converging in one extra attempt instead of potentially
     re-discovering and re-abandoning over the same gap repeatedly.
   - New deterministic test,
     `test_compiler_releases_guard_lock_when_earlier_sorting_guard_discovered`: seeds a
     reconcile with only ticker "Z", reloads to introduce earlier-sorting ticker "A" right
     after Z's guard is acquired, and uses a second connection's non-blocking
     `pg_try_advisory_lock` to prove Z's guard is held at the moment of discovery and
     released before the retry's fresh connection is even used. Calls `reconcile_alerts`
     directly (not through `reconcile_structural_alerts`) so unrelated connections from
     `_held_symbols()`/`backfill_legacy_structural_alerts()` don't interfere with the
     connection-count check. Confirmed this test fails against the pre-fix code (reverted
     the new ordering check, got a `KeyError` because no retry ever happened, restored the
     fix, reran clean).
   - Full isolated-DB regression: **199/199 tests passed** (198 prior + 1 new), restored
     backup's 92 alerts/50 controls unchanged, scratch DB dropped after. Production
     untouched throughout.

   **Group C — fixed.** `dashboard/pages/P10_Morning_Brief.py`:
   - The baseline used for conflict detection (`baseline_kl`) was being recomputed as
     `copy.deepcopy(load_key_levels_from_db())` on *every* rerun — i.e. tied to each
     rerun's current DB state, not to what the widgets actually show. Since Streamlit's
     `number_input(value=..., key=...)` only uses `value` the first time a key appears in
     `session_state`, the widgets themselves don't refresh to a concurrent DB change
     either — so the baseline needs the same "pin on first load" semantics the widgets
     already have, or CAS comparisons silently compare the wrong thing. Fixed: the
     baseline is now stored once in `st.session_state["p10_kl_baseline"]` the first time
     Tab 2 renders in a session, and only ever updated again after a *successful* save (to
     that save's own values) — never re-read from the DB on an unrelated rerun.
   - Added a **🔄 Refresh from DB** button (`key="p10_refresh_button"`) that explicitly
     drops both the pinned baseline and all per-ticker widget keys together, so the only
     way stale values go away is a deliberate action, not an accidental silent resync.
   - Added `_reset_key_levels_session_state()`, also called after "Sync Positions from
     DB" succeeds (a different code path that changes key_levels outside Tab 2's own
     save), so Tab 2 doesn't carry a baseline that's now stale relative to that sync.
   - **Misleading success fixed everywhere a save result is shown:** "Save All", both
     "Add Position"/"Add Watch Level" expander forms, and the sidebar "Sync Positions from
     DB" button all previously called `st.success(...)`/built a success message
     unconditionally, only noting an `"error"` key (if any) as an aside inside the success
     text. All four now check `stats.get("error")` first and render `st.error("Not
     saved/added: ...")` — never a success banner — when the save didn't happen. Added
     `_flash_from_stats()` to make this one shared code path for "Save All" instead of
     ad hoc per-call-site logic.
   - New browser test (Streamlit `AppTest`, `test_p10_browser_stale_save_shows_error_not_success`
     in `tests/test_journal_review_db.py`): loads the real P10 page against the isolated
     test schema, confirms the widget shows the originally-loaded stop (90), changes the
     underlying DB value (95) behind the session's back, forces an unrelated rerun
     (toggling a checkbox) and confirms the widget/baseline did *not* silently pick up 95,
     then clicks Save All and confirms it renders an `st.error` ("Not saved: ...") and
     *no* success banner, and that the concurrently-set value (95) survived untouched.
     Finally clicks Refresh from DB and confirms the widget now shows 95. Confirmed this
     test fails without the baseline-pinning fix (reverted it, got `AssertionError: []
     is not true: expected an error alert...` — i.e. no conflict was ever detected because
     the baseline silently resynced to 95 too — restored the fix, reran clean).
   - Full isolated-DB regression: **200/200 tests passed** (199 prior + 1 new), restored
     backup's 92 alerts/50 controls unchanged, scratch DB dropped after. Production
     untouched throughout.

   **Group C follow-up — reporting gap found and fixed.** Caught on review:
   `save_structural_key_levels` commits the key_levels write *before* calling
   `reconcile_structural_alerts` to refresh alerts. If that reconcile call then raised, the
   exception propagated out uncaught, and P10's wrapper reported it exactly like a CAS
   conflict — `"Not saved: ..."` — even though the key_levels write had already committed.
   A user seeing "Not saved" and retrying would then likely hit a *second*, confusing CAS
   conflict (their own already-saved values now differ from their stale baseline).
   - Fixed: `save_structural_key_levels` now wraps the `reconcile_structural_alerts` call
     in its own try/except, distinct from the CAS-conflict path above it. A reconcile
     failure returns a stats dict with `"key_levels_saved": True` alongside the error,
     instead of letting the exception propagate indistinguishably from a full rejection.
   - P10's `_flash_from_stats` now branches on this flag: `"Not saved: ..."` only for a
     true rejection (nothing written); `"Key levels saved, but alert refresh failed: ...
     — refresh this page or retry to reconcile alerts."` for a partial completion. Applied
     consistently across all four call sites (Save All, both "Add" forms, and the sidebar
     "Sync Positions from DB" button) — all now update the pinned baseline whenever
     `key_levels_saved` is true, regardless of whether the alert refresh itself succeeded,
     since the saved values are real and a later save must compare against them, not the
     pre-save baseline.
   - New backend test, `test_p10_save_distinguishes_rejected_from_partial_completion`:
     forces `reconcile_structural_alerts` to raise after a successful key_levels commit,
     confirms the returned stats have `key_levels_saved=True` and the value really is
     persisted — then contrasts with a genuine CAS-conflict rejection, confirming nothing
     changes in that case.
   - New browser test, `test_p10_browser_partial_completion_distinguishes_from_rejection`:
     same scenario through the real P10 page — confirms the rendered message says "Key
     levels saved... alert refresh failed", never starts with "Not saved", and the DB
     value matches what was actually submitted.
   - Confirmed both new tests fail against the pre-fix code (reverted the try/except in
     `save_structural_key_levels`, got the expected failures — backend test raised an
     uncaught `RuntimeError`, browser test's message read `"Not saved: alert refresh
     boom"` — restored the fix, reran clean).
   - Fixed a self-inflicted test-file splicing bug caught while building this: an earlier
     edit had merged the end of `test_p10_browser_stale_save_shows_error_not_success`
     (its "Refresh from DB" check) into the new test instead of leaving it in place.
     Caught by `grep -c "def test_"` not matching the expected count and by the new test's
     assertions failing in a way that didn't match its own scenario; corrected before
     final verification.
   - Full isolated-DB regression: **202/202 tests passed** (200 prior + 2 new), restored
     backup's 92 alerts/50 controls unchanged, scratch DB dropped after. Production
     untouched throughout.

   **Migration and rollout: untouched this round, per instruction.**
8. [x] Implemented locally: sync broker before journal in the morning pipeline. Holding lookup failure blocks
   stop approval. Brief shows watch ideas separately from active alerts and pending count.
   Honest reporting: extracted ideas, pending, activated, replacements, validation issues.
9. [ ] Test mentions/SMH/watch bypass, evidence/timeframes/ranges, repeat/reject/purge behavior,
   supported stops/holdings failures, stale previews/rollback/controls, safe pipeline order,
   and UI selection. Run full unit and isolated DB/browser suites with SMTP/quotes mocked.
   Latest full run: 186 tests passed (no skips), including existing and new isolated
   PostgreSQL/Streamlit tests. Additional gaps above must be verified before final sign-off.
10. [ ] Apply verified migration after backup, restart Streamlit, resync latest journal once
   into review, verify no live alerts changed, then browser-test selective approval/rejection
   and repeat-sync deduplication. Existing SMH/other alerts are not automatically removed.
11. [ ] Record acceptance, then commit/push/PR/merge only when explicitly requested.

Rollback: retain additive proposal/audit storage. Do not revert journal sync to automatic
activation; leave notifications and existing manual source edits independent of review.

#### Handoff snapshot — October 11, 2026

**Git / deployment state**

- Current branch: `feature/journal-alert-approval`, tracking the same branch on origin.
  This handoff was prepared before the WIP checkpoint. The user subsequently authorized
  committing/pushing the implementation and these notes on October 11; consult git log
  and remote status for the checkpoint. Preserve any later dirty worktree; do not discard
  changes or recreate the work from scratch.
- Base is updated main after PR #86 (`062d011`). No PR/merge for this round; checkpoint
  authorization is for saving WIP only, not release acceptance.
- No production migration 077, actual journal re-extraction, approval, alert activation,
  source-level change, live SMTP test, or trade has been performed in this round.
- Existing SMH alerts #140/#141 and all prior live alerts/levels remain unchanged.
  Scheduled poller is still live; don't invoke it as part of acceptance tests.

**Code delivered locally**

- `data_model/077_journal_review.sql`: additive items, evidence and decision audit tables;
  sync-log extraction version/count metadata; no live alert/source backfill or deletion.
  `alerts/migrate.py --journal-review` selects this targeted migration.
- `morning_brief/journal_review.py`: normalization/evidence checks, condition fingerprints,
  staging-only ingestion, durable review history, previews, prepare/reject/approve service,
  operator-owned manual alerts, exact-existing linking, manual replacements and holding-stop
  approvals. Shared poll/manual/purge locks and row/version checks; whole batches roll back.
- `morning_brief/journal_sync.py`: one existing LLM call with evidence/timeframe instructions;
  preflight migration check; no compiler or live key-level writes. Legacy syncs re-extract
  once under extraction version 2. Dry-run requires no database and performs no writes.
- `morning_brief/alert_compiler.py`: normal journal reconciliation no longer calls automatic
  promotion. Legacy reconcile/promotion helper functions still exist; inspect callers if
  extending this boundary, rather than assuming those helpers were removed.
- `dashboard/journal_review_ui.py` and P10: new Journal Review tab, context/idea/pending/
  blocked/reviewed views, symbol grouping/search, unchecked selections, existing-alert
  comparisons, quote refresh only on request, explicit timeframe clarification, preview,
  cancel and two approval confirmations. Reviewed items show linked alert IDs.
- `dashboard/morning_pipeline.py`: E*TRADE sync → journal staging → brief; stale-token and
  partial-sync results remain visible. Corresponding order tests updated.
- Brief adds a separate watch-idea/pending-count section, capped at eight ideas, outside
  the LLM payload. Sync History labels extracted ideas rather than claiming activation.
- README updated; CLAUDE.md records Shift+Tab CLI mode switching. Historical PLAN content
  remains intact. New tests: `tests/test_journal_review.py`, `tests/test_journal_review_db.py`;
  existing pipeline/Claude/brief/compiler-control tests updated for the new behavior.

**Implemented defaults / limitations to retain or explicitly review**

- New/replaced manual watch alerts default to 21 days; UI permits 1–3650 days. Stops have
  no expiry. Same-condition stop approval does not reset the triggered flag.
- Holdings must be positive EQ positions in latest account snapshots within FOUR calendar
  days, allowing weekend review. Missing/stale holdings block stop approval. This is a
  timestamp/ownership check, not proof of a complete fresh sync of every account.
- Only simple above/below price conditions are executable. Daily/weekly/sustained/range/
  unspecified ideas require explicit user choice of the DISPLAYED threshold as a simple
  price crossing. This is NOT an implementation of close/confirmation/timeframe polling.
- Evidence must be a literal excerpt (whitespace-normalized) identifying instrument and
  numeric level. Index→futures substitution is blocked. Evidence date currently comes
  from the last ISO date in the filename; otherwise it is unknown. Older-than-21-day
  evidence gets a UI warning, not automatic deletion or activation.
- Fingerprints use exact symbol/condition/value/timeframe and separate stop intent; they
  do not silently merge nearby levels. Rejected/accepted matches never revive on wording
  changes. Material new values/timeframes appear separately for human review.
- Invalid extraction is blocked; UI has no free-form evidence editor. Correct the journal
  and resync, or manage a deliberate manual alert in P11. Purged managed stops require a
  separate restoration workflow (not built); approval cannot silently unblock them.
- Quotes are explicit Yahoo snapshots, potentially missing/delayed; approval acknowledges
  evidence/timing risk. Approval itself sends no email, but a later scheduled poll can.
- Existing legacy alerts are not auto-cleaned. New watch ideas do not enter live key_levels;
  accepted watch alerts are manual/operator-owned, with journal provenance in audit records.
- Final safety review should cover concurrent compiler/P10 source changes and source-stop
  consistency, invalid/conflicting batches and shared lock ordering. Do not assume all
  concurrency behavior is proven solely because the current regression passes.

**Backup and verification evidence**

- Pre-change backup (no older backups pruned):
  `/Users/davidliao/Library/CloudStorage/Dropbox/Etrade/db_backups/fifth_dragon_capital_20261011_201812.dump`.
- Last successful full run: 186 tests, no failures/skips. Quotes and SMTP were mocked.
  New DB/browser coverage includes staging without live writes, accepted/rejected repeats,
  default expiry, preparation/clarification, stale-preview batch rollback, paused-target
  replacement, exact-existing no-op, verified/stale holding stops, purged-stop refusal,
  repeat migration, real select/preview/cancel/two-confirmation approval buttons, invalid
  batch rollback and unchanged-stop rearm protection.
- Fresh backup restored in an isolated temporary database. Migrations 075/076/077 applied
  twice; 92 original alert rows and all key-level rows unchanged, 50 prior controls retained.
  Only the temporary verification database was removed afterward; production untouched.
- Python: `/Users/davidliao/git_repos/py312/venv/bin/python`.
  Unit/default run: `python -m unittest discover -s tests -q` (DB tests skip without opt-in).
  Full isolated harness: `python /private/tmp/verify_alert_task1.py`; this UNTRACKED temporary
  helper may not survive handoff to another machine. It creates/restores a scratch database,
  sets ALERT_TEST_DATABASE_URL to that database, runs all tests, then drops only its own
  scratch database. Review it before use; never set ALERT_TEST_DATABASE_URL to production.
- `python -m compileall -q morning_brief dashboard alerts tests` and `git diff --check`
  passed at intermediate checkpoints; rerun both after any further edits.

**Next section / checkpoints — WAIT FOR USER DIRECTION**

1. Report this handoff and STOP. Do not implement the remaining steps automatically.
2. Suggested next single section: finish/review Step 5, specifically suppression of prior
   purged watch/journal conditions in the new queue. Add targeted tests, report results,
   and STOP again. Step 7 concurrency review is a subsequent separate checkpoint.
3. Only after approval of verification and rollout: compare live alert/level baselines,
   take a current backup if needed, apply `python -m alerts.migrate --journal-review`,
   and confirm baseline data unchanged. No live journal extraction as part of migration.
4. Browser handoff: restart `streamlit run dashboard/app.py`; P10 Sync Latest Journal
   re-extracts the weekly journal once into review. Verify live alert/level counts stay
   unchanged and SMH becomes watch evidence, not new notifications. Test a selected prepare/
   reject/approve action, expiry, preserved controls, quote warnings and repeat deduplication.
   Real approval can lead to subsequent scheduled notification: user selects the targets.
5. User authorized a WIP checkpoint commit/push on October 11 for portability. That does
   not authorize deployment, PR/merge, or further implementation. Later acceptance/release
   commits still require explicit direction and the one-section-at-a-time checkpoints.

Date: October 8, 2026; acceptance updated October 9, 2026.
Status: Priorities 1–2 accepted by the user (Release A). Priority 1 was pushed as `a2e6b7d`;
Priority 2 and the October 9 extensions are approved for commit, PR, and merge after full tests.
Release branch: `feature/p7-alert-management`, created from `main`.
Release A merged through PR #85; next-stage branch `feature/brief-alert-suggestions`
was created and pushed from updated main. Priority 3 was revised below on October 9;
Priority 4 remains deferred. Acceptance does not authorize unattended alert changes.

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
   - [x] Replace long dropdowns with searchable/selectable rows and a detail editor.
   - [x] Display current price, distance, source, lifecycle, expiry, and poll health.
   - [x] Provide safe manual creation/editing and confirmed scoped bulk actions.
   - [x] Keep P7 charts/overlays, moving detailed management to an Alerts workspace.
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

Implementation record — October 8, 2026:

- Added `P11_Alerts.py`, shared `dashboard/alerts_workspace.py` presentation helpers,
  and `alerts/management.py` validated mutations. No new migration or production alert
  edits were needed for the initial workspace task. The October 9 purge extension added
  migration 076; later user-directed holdings cleanup is recorded below. Scheduler is unchanged.
- Table provides All alerts / Needs attention / History, combined search/filters, native
  row selection, manual details, price/distance/retrieval time, source, lifecycle, expiry,
  and delivery/attention state. Quotes refresh explicitly, not on selection or filter changes.
- Bulk operations preview exact IDs, block invalid mixed selections, require confirmation,
  lock/revalidate configuration and lifecycle, and commit all-or-nothing. Cancel is read-only.
  Manual create/edit detects exact duplicates and preserves operator suppression.
- Exact duplicate consolidation preserves the structural row and archives/suppresses only
  the confirmed manual/journal duplicate. Changed pairs are rejected; near matches stay
  review-only. Hygiene queries load on demand and exclude ineligible alerts.
- P7 retains charts and fresh eligible overlays with links to P11. Detailed management and
  alert-ranking quote requests were removed. No numerical latency improvement is claimed;
  automated checks verify selection/actions do not invoke quote fetches or chart rendering.
- Verification: 102 unit tests plus 24 isolated PostgreSQL/Streamlit tests. The grid-selection
  event is simulated in AppTest (native selection is checked separately by pure mapping tests);
  real button flows and database mutations are exercised. SMTP/quotes are mocked in tests.

User browser acceptance:

- [ ] Restart Streamlit and open P11 Alerts from the sidebar or P7 link.
- [ ] Refresh quotes once; search/filter/select a row and confirm charts/quotes do not reload.
- [ ] Select two appropriate alerts, preview Pause, cancel once, then confirm/apply. Verify
  only the previewed IDs changed. Select paused rows and repeat with Resume.
- [ ] Inspect a manual row, edit its label/level, and verify source-managed rows remain read-only.
- [ ] Verify Needs attention/History, on-demand hygiene reports, and P7 chart overlays.
- [ ] Confirm native grid sorting/selection identifies the intended IDs before applying actions.

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
- [x] Per October 9 user direction, provide bulk permanent Purge for every source/state,
  with selected-ID preview and confirmation. Retain audit snapshots and block recreation
  of purged managed identities/ideas; do not remove authoritative trading levels.
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

### Priority 3 — Journal-independent hygiene first (revised October 9)

The user is not maintaining daily journals and approved a journal-independent approach.
Start with deterministic exception review using stored alerts and current market data,
without extra LLM calls or automatically changing levels. The original Phases 7–8 design
below is retained as a deferred proposal, not the current implementation commitment.

- [x] Implement on-demand P11 exception review for invalid levels, expired ideas, old
  recorded evidence, and configurable large price distances (default 25% / 90 days).
- [x] Exclude archived alerts by default; provide an explicit inclusion option.
- [x] Fetch quotes explicitly and investigate distant equity alerts using up to two years
  of split evidence; exclude futures/indices from stock-split checks. Clearly label split
  mismatches as heuristics, with dates/ratios and no automatic numerical correction.
- [x] Do not treat routine compiler refreshed_at as evidence of fresh trading intent.
- [x] Preserve all alert/lifecycle settings; snapshot report survives reruns and contains
  IDs, source, status, reasons, evidence dates, price distances, and suggested review steps.
- [x] Full regression: 152 tests passed, including 32 isolated PostgreSQL/Streamlit tests.
  Browser automation verified review is explicit, read-only, retains its snapshot across
  filtering, and sends no emails. Migration repeat checks and `git diff --check` passed.
- [ ] User browser acceptance: open P11 Hygiene reports, run exception review, check
  evidence/reasons and adjustable distance/age rules; confirm existing alerts stay unchanged.
- [x] User approved a small read-only brief summary after discovering old UUUU stop #46
  was not visible in the brief. Add Protect holdings / Watch opportunities / Resolve
  ambiguity near the top, with per-group caps/counts, source-level review intent,
  daily-bar date, independent last-poll flag, lifecycle and delivery outcome.
- [x] Keep old stops without holdings visible as ambiguous re-entry-watch/update/retire
  decisions; do not infer buy intent or automatically archive/delete them.
- [x] Missing/stale prices cannot claim current condition; missing holdings produce an
  unavailable notice. Local alert snapshot is never appended to the LLM payload.
- [x] Attention-summary verification: full suite 161 tests passed, including 32 isolated
  database/browser tests. Added closed-stop ambiguity, held stops/nearby levels, watch
  crossings, stale/missing prices, suppression/expiry, list caps, price/poll disagreement,
  read-only connection, holdings failure, and top-of-brief placement checks.
- [x] Browser acceptance: regenerate Brief only, verify UUUU #46 under Resolve ambiguity,
  review protection/opportunity groups and price/poll distinctions; no alert changes.
- [x] User-requested presentation refinement: separate Alert and Price snapshot labels;
  color status badges (red held stop breach, green unheld watch crossing, amber review,
  gray unmet/unknown/inactive); keep poll/delivery details quieter and show retrieval time.
- [x] User accepted the colored, labeled summary after regeneration and requested commit/push.
  Full regression: 163 tests passed, including 32 isolated database/browser tests;
  `git diff --check` passed. Summary remains read-only; no extra LLM calls.
- [ ] Assess usefulness before a durable review queue or more UI redesign.
- [ ] Optional later stage: calculated highs/lows or moving-average proposals, explicitly
  labeled calculations rather than journal/trading intent. Requires separate approval.

### Deferred original Priority 3 — Opt-in brief-assisted maintenance

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

### October 9 — Task 2 bulk-purge extension

User explicitly requested removing any selected alerts, including managed, archived, and
expired rows. This supersedes the initial preference for archive-only management.

- [x] Implement all-or-nothing bulk Purge with locked targets and stale-preview checks.
- [x] Retain alert/notification snapshots in audit history; delete workspace rows.
- [x] Add additive migration 076 to prevent compiler recreation of purged managed ideas.
- [x] Add minimum-distance filtering and flag levels at least 100% from the quote.
- [x] Test all sources/states, compiler refresh/promotion, invalid levels, stale targets,
  notification snapshots, and transaction rollback in an isolated database.
- [x] User browser acceptance: refresh quotes, select multiple rows, preview/confirm Purge,
  verify count decreases and managed alerts remain absent after source refresh.
- [ ] Commit/push only after acceptance and explicit instruction.

### October 9 — Held-symbol alert policy (user-approved)

- [x] Use actual broker holdings rather than retained Positions metadata.
- [x] Exclude held symbols from structural watch/support/resistance and journal generation.
- [x] Archive existing managed non-stop alerts on reconciliation; retain saved levels.
- [x] Preserve configured position-stop alerts, manual alerts, and durable operator controls.
- [x] Allow watch alerts to return after exit unless purged/suppressed; journal ideas need sync.
- [x] Verify holdings-error abort, dry-run rollback, held/unheld transitions, and source
  isolation. 106 unit tests and 31 isolated PostgreSQL tests passed; no live emails sent.
- [x] Browser acceptance: P10 Save All, P11 refresh, check held watch/journal alerts are
  archived while stop/manual alerts remain; non-held watch alerts remain available.

### October 9 — Release A acceptance and next-stage handoff

- [x] User reports alert management/purge and held-symbol policy working and accepts release.
- [x] Brief Positions now displays actual holdings only; saved metadata cannot add closed
  positions. Removed obsolete saved VIXY entry after backup; broker history was not deleted.
- [x] Centralized brief-only exclusions RBL/RPI/BR in `BRIEF_POSITION_EXCLUSIONS`.
- [x] User accepted 1/5/20-session colors and independently colored 20/50-MA comparisons.
  Percentage returns use green positive, amber −2.5% through zero, red below −2.5%;
  non-percentage changes use sign only. Colors describe direction, not investment advice.
- [x] User authorized full regression, commit/push, PR merge, and a fresh development branch.
- [x] Final full regression: 142 tests passed, including 31 isolated PostgreSQL/Streamlit
  tests; none skipped in the full run. Restored backup verification applied migrations
  075/076 twice with existing alert rows unchanged. SMTP and quote dependencies mocked.
  `git diff --check` passed. Production data was not modified during regression.
- [x] Merge Release A through GitHub PR #85; do not commit directly to main.
- [x] Create/push `feature/brief-alert-suggestions` from updated main for Priority 3.

Next stage remains opt-in structured suggestions with provenance, local validation, and
explicit Accept/Keep/Snooze review. Priorities 3–4 remain unimplemented. The original June 2
plan is preserved above; earlier implementation test totals are historical checkpoints.
