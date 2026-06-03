-- Normalized event log of all financial activity.
-- Sourced from transactions (and optionally orders in future).
-- Grain: one row per financial event line.
--   source_line_id handles fan-out: a single order can produce multiple ledger
--   rows (e.g. partial fills, multi-leg spreads). Defaults to '0' for
--   transaction-sourced rows where there is always a 1:1 mapping.
CREATE TABLE IF NOT EXISTS ledger (
    id             SERIAL PRIMARY KEY,
    account_id_key TEXT NOT NULL,
    event_timestamp TIMESTAMPTZ NOT NULL,   -- execution/posting time (not just date)
    settlement_date DATE,                   -- T+1 / T+2 settlement; NULL for cash events
    event_type     TEXT NOT NULL,           -- buy | sell | dividend | dividend_qualified |
                                            -- interest | fee | transfer | deposit |
                                            -- withdrawal | split | redemption
    symbol         TEXT,
    quantity       NUMERIC,                 -- positive = acquired, negative = disposed
    price          NUMERIC,                 -- per share/unit
    gross_amount   NUMERIC,                 -- abs(quantity × price)
    net_amount     NUMERIC,                 -- cash impact (neg = paid out, pos = received)
    fee            NUMERIC DEFAULT 0,
    currency       TEXT DEFAULT 'USD',
    source_table   TEXT NOT NULL,           -- 'transactions' or 'orders'
    source_id      TEXT NOT NULL,           -- transaction_id or order_id
    source_line_id TEXT NOT NULL DEFAULT '0', -- leg/line within source row; '0' for 1:1 events
    raw            JSONB,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_table, source_id, source_line_id)
);

CREATE INDEX IF NOT EXISTS ledger_account_date  ON ledger (account_id_key, event_timestamp);
CREATE INDEX IF NOT EXISTS ledger_symbol        ON ledger (symbol);
CREATE INDEX IF NOT EXISTS ledger_event_type    ON ledger (event_type);
