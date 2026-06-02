CREATE TABLE IF NOT EXISTS transactions (
    id               SERIAL PRIMARY KEY,
    account_id_key   TEXT NOT NULL,
    transaction_id   TEXT UNIQUE NOT NULL,
    transaction_date TIMESTAMPTZ,
    transaction_type TEXT,
    description      TEXT,
    description2     TEXT,
    amount           NUMERIC,
    symbol           TEXT,
    quantity         NUMERIC,
    price            NUMERIC,
    fee              NUMERIC,
    settlement_date  TIMESTAMPTZ,
    raw              JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
