-- Raw ingest provenance for transaction rows.
-- This keeps source fingerprints and duplicate-classification history separate
-- from the normalized ledger so future rebuilds can choose canonical rows
-- without relying on heuristic deletes.

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS source_system TEXT NOT NULL DEFAULT 'api',
    ADD COLUMN IF NOT EXISTS source_record_key TEXT,
    ADD COLUMN IF NOT EXISTS source_payload_hash TEXT,
    ADD COLUMN IF NOT EXISTS canonical_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS dedupe_signature TEXT;

CREATE INDEX IF NOT EXISTS transactions_source_system_key
    ON transactions (source_system, source_record_key);

CREATE INDEX IF NOT EXISTS transactions_source_payload_hash
    ON transactions (source_payload_hash);

CREATE INDEX IF NOT EXISTS transactions_canonical_fingerprint
    ON transactions (canonical_fingerprint);

CREATE INDEX IF NOT EXISTS transactions_dedupe_signature
    ON transactions (dedupe_signature);

CREATE TABLE IF NOT EXISTS transaction_ingest_audit (
    id                    SERIAL PRIMARY KEY,
    account_id_key        TEXT NOT NULL,
    transaction_id        TEXT NOT NULL,
    source_system         TEXT NOT NULL,
    source_record_key     TEXT,
    source_payload_hash   TEXT NOT NULL,
    canonical_fingerprint TEXT NOT NULL,
    dedupe_signature      TEXT NOT NULL,
    write_status          TEXT NOT NULL,
    classification        TEXT NOT NULL,
    reason                TEXT,
    peer_transaction_ids  JSONB,
    peer_source_systems   JSONB,
    payload_summary       JSONB,
    observed_count        INTEGER NOT NULL DEFAULT 1,
    last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE IF EXISTS transaction_ingest_audit
    ADD COLUMN IF NOT EXISTS observed_count INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS transaction_ingest_audit_account_created
    ON transaction_ingest_audit (account_id_key, created_at DESC);

CREATE INDEX IF NOT EXISTS transaction_ingest_audit_source_row_key
    ON transaction_ingest_audit (account_id_key, source_system, source_record_key);

CREATE INDEX IF NOT EXISTS transaction_ingest_audit_signature
    ON transaction_ingest_audit (account_id_key, dedupe_signature);
