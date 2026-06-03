-- Records every pipeline job run: sync, build-ledger, seed-*, and token_stale events.
-- Dashboard reads this table for job history, status, and duration.
CREATE TABLE IF NOT EXISTS sync_log (
    id           SERIAL PRIMARY KEY,
    job_name     TEXT NOT NULL,         -- sync | sync:accounts | build_ledger | seed_symbols | seed_dates | token_stale
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    status       TEXT,                  -- running | success | failed | token_stale
    duration_s   NUMERIC,
    rows_synced  JSONB,                 -- {"accounts": 4, "transactions": 20, ...}
    error_msg    TEXT,
    triggered_by TEXT DEFAULT 'manual'  -- manual | launchd
);

CREATE INDEX IF NOT EXISTS sync_log_started ON sync_log (started_at DESC);
CREATE INDEX IF NOT EXISTS sync_log_job     ON sync_log (job_name, started_at DESC);
