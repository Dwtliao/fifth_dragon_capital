-- 072: Add recurrence tracking for journal alert promotion (#59).
-- recurrence_count: how many sync runs have matched this idea (starts at 1 on create)
-- first_seen_at: when this idea first appeared
-- last_seen_at: when this idea most recently reappeared

ALTER TABLE price_alerts
    ADD COLUMN IF NOT EXISTS recurrence_count SMALLINT     NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS first_seen_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_seen_at      TIMESTAMPTZ;
