-- Account dimension with SCD Type 2 support.
-- account_sk is the surrogate key that fact tables reference — pointing to a
-- specific version row makes temporal joins implicit. account_id_key is the
-- natural/business key shared across versions.
-- When account attributes change: close old row (set effective_to, is_current=FALSE),
-- insert new row with updated attributes and is_current=TRUE.
CREATE TABLE IF NOT EXISTS dim_accounts (
    account_sk       SERIAL PRIMARY KEY,
    account_id_key   TEXT NOT NULL,
    account_id       TEXT,
    account_name     TEXT,
    account_mode     TEXT,              -- CASH | MARGIN
    account_type     TEXT,             -- INDIVIDUAL | IRA | ROTH_IRA | etc.
    institution_type TEXT,
    status           TEXT,             -- ACTIVE | CLOSED
    effective_from   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_to     TIMESTAMPTZ,      -- NULL = currently active version
    is_current       BOOL NOT NULL DEFAULT TRUE,
    updated_at       TIMESTAMPTZ DEFAULT NOW(),

    -- is_current and effective_to must agree
    CONSTRAINT chk_scd2_consistency CHECK (
        (is_current = TRUE  AND effective_to IS NULL) OR
        (is_current = FALSE AND effective_to IS NOT NULL)
    )
);

-- Only one current version per account at a time
CREATE UNIQUE INDEX IF NOT EXISTS dim_accounts_one_current
    ON dim_accounts (account_id_key) WHERE is_current = TRUE;

CREATE INDEX IF NOT EXISTS dim_accounts_key ON dim_accounts (account_id_key);

-- Supports efficient as-of temporal joins:
--   WHERE account_id_key = X AND effective_from <= t AND (effective_to IS NULL OR effective_to > t)
CREATE INDEX IF NOT EXISTS dim_accounts_asof
    ON dim_accounts (account_id_key, effective_from DESC, effective_to);

-- Convenience view: current account attributes only.
-- BI queries that don't need historical account state should use this.
CREATE OR REPLACE VIEW dim_accounts_current AS
SELECT account_sk, account_id_key, account_id, account_name,
       account_mode, account_type, institution_type, status,
       effective_from, updated_at
FROM dim_accounts
WHERE is_current = TRUE;
