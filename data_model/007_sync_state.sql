CREATE TABLE IF NOT EXISTS sync_state (
    id               SERIAL PRIMARY KEY,
    account_id_key   TEXT NOT NULL,
    data_type        TEXT NOT NULL,
    last_synced_at   TIMESTAMPTZ,
    last_marker      TEXT,
    UNIQUE (account_id_key, data_type)
);
