-- 069: Add source, tier, expires_at to price_alerts
-- source: where the row came from (manual / journal_sync / key_levels_watch)
-- tier:   1 = tied to a position stop (structural), 2 = watch level, 3 = journal musing
-- expires_at: NULL = never expires; set by journal_sync for time-limited alerts

ALTER TABLE price_alerts
    ADD COLUMN IF NOT EXISTS source      TEXT        NOT NULL DEFAULT 'manual'
                                         CHECK (source IN ('manual', 'journal_sync', 'key_levels_watch')),
    ADD COLUMN IF NOT EXISTS tier        SMALLINT    NOT NULL DEFAULT 2,
    ADD COLUMN IF NOT EXISTS expires_at  TIMESTAMPTZ;

-- Back-fill: everything existing is treated as manual.
-- Rows with no label or that are stale-triggered get a 30-day expiry so the
-- prune cycle clears them out naturally rather than requiring manual deletion.
UPDATE price_alerts
SET expires_at = NOW() + INTERVAL '30 days'
WHERE
    label IS NULL
    OR (
        triggered = TRUE
        AND last_fired_at IS NOT NULL
        AND last_fired_at < NOW() - INTERVAL '30 days'
    );
