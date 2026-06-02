-- Fact table: cash-only events (deposits, withdrawals, dividends, interest, fees).
-- Derived as a filtered subset of the ledger (events with no share quantity).
-- running_balance is intentionally omitted — it's derived state that drifts
-- on backfills; compute it at query time with a window function:
--   SUM(amount) OVER (PARTITION BY account_id_key ORDER BY event_timestamp)
-- ledger_id is NOT NULL UNIQUE: each cashflow row maps to exactly one ledger row.
CREATE TABLE IF NOT EXISTS fact_cashflows (
    id              SERIAL PRIMARY KEY,
    date_key        DATE NOT NULL REFERENCES dim_dates(date_key),
    account_sk      INT  NOT NULL REFERENCES dim_accounts(account_sk),
    account_id_key  TEXT NOT NULL,
    symbol          TEXT,               -- NULL for pure cash; ticker for dividends
    event_type      TEXT NOT NULL,      -- deposit | withdrawal | dividend | interest | fee
    event_timestamp TIMESTAMPTZ NOT NULL,
    amount          NUMERIC NOT NULL,   -- positive = cash in, negative = cash out
    ledger_id       INT  NOT NULL UNIQUE REFERENCES ledger(id),
    source_table    TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    UNIQUE (source_table, source_id)
);

CREATE INDEX IF NOT EXISTS fact_cf_date    ON fact_cashflows (date_key);
CREATE INDEX IF NOT EXISTS fact_cf_account ON fact_cashflows (account_sk);
