-- 070: Add lifecycle fields for compiler-managed alerts.
-- source_key: stable per-source identity used for reconciliation
-- refreshed_at: last time the compiler touched the row
-- archived_at: when the row was retired by the compiler
-- pinned: manual-exception marker; compiler-managed rows leave this false

ALTER TABLE price_alerts
    ADD COLUMN IF NOT EXISTS source_key    TEXT,
    ADD COLUMN IF NOT EXISTS refreshed_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS archived_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pinned        BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS price_alerts_source_key_idx
    ON price_alerts (source, source_key);

CREATE INDEX IF NOT EXISTS price_alerts_active_idx
    ON price_alerts (source, enabled, archived_at);
