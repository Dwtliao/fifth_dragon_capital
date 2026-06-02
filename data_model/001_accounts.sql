CREATE TABLE IF NOT EXISTS accounts (
    id               SERIAL PRIMARY KEY,
    account_id_key   TEXT UNIQUE NOT NULL,
    account_id       TEXT,
    account_name     TEXT,
    account_desc     TEXT,
    account_mode     TEXT,
    account_type     TEXT,
    institution_type TEXT,
    status           TEXT,
    closed_date      TIMESTAMPTZ,
    raw              JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);
