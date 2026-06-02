-- Fact table: one row per financial event, keyed to dimension tables.
-- Derived from ledger. date_key is the execution date for slicing; event_timestamp
-- preserves full precision for same-day ordering and settlement reconciliation.
-- account_sk references a specific dim_accounts version row (SCD2 surrogate),
-- making temporal joins implicit. account_id_key is retained for convenience.
-- ledger_id is NOT NULL UNIQUE: each fact row maps to exactly one ledger row.
CREATE TABLE IF NOT EXISTS fact_transactions (
    id              SERIAL PRIMARY KEY,
    date_key        DATE NOT NULL REFERENCES dim_dates(date_key),
    account_sk      INT  NOT NULL REFERENCES dim_accounts(account_sk),
    account_id_key  TEXT NOT NULL,
    symbol          TEXT REFERENCES dim_symbols(symbol),
    event_type      TEXT NOT NULL,
    event_timestamp TIMESTAMPTZ NOT NULL,
    settlement_date DATE,
    quantity        NUMERIC,
    price           NUMERIC,
    gross_amount    NUMERIC,
    net_amount      NUMERIC,
    fee             NUMERIC DEFAULT 0,
    ledger_id       INT  NOT NULL UNIQUE REFERENCES ledger(id),
    source_table    TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    source_line_id  TEXT NOT NULL DEFAULT '0',
    UNIQUE (source_table, source_id, source_line_id)
);

CREATE INDEX IF NOT EXISTS fact_txn_date    ON fact_transactions (date_key);
CREATE INDEX IF NOT EXISTS fact_txn_symbol  ON fact_transactions (symbol);
CREATE INDEX IF NOT EXISTS fact_txn_account ON fact_transactions (account_sk);
