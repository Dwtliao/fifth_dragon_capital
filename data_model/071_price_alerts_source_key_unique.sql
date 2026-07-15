-- 071: Add a unique constraint after backfill.
-- This should be applied only after the live rows have source_key values.

CREATE UNIQUE INDEX IF NOT EXISTS price_alerts_source_key_unique
    ON price_alerts (source, source_key)
    WHERE source_key IS NOT NULL;
