-- Purged rows leave the workspace permanently; remember managed-source intent.
CREATE TABLE IF NOT EXISTS alert_purge_blocks (
    id BIGSERIAL PRIMARY KEY,
    original_alert_id INTEGER NOT NULL UNIQUE,
    source TEXT NOT NULL,
    identity TEXT NOT NULL,
    ticker TEXT NOT NULL,
    condition TEXT NOT NULL,
    threshold NUMERIC NOT NULL,
    legacy_identity BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS alert_purge_blocks_source_idx
    ON alert_purge_blocks (source, identity);
CREATE INDEX IF NOT EXISTS alert_purge_blocks_symbol_idx
    ON alert_purge_blocks (source, ticker, condition);

CREATE OR REPLACE FUNCTION prevent_purged_alert_recreation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source = 'manual' THEN RETURN NEW; END IF;
    -- Serialize source insertion with a purge of the same source/symbol/direction.
    IF TG_OP = 'INSERT' THEN
        PERFORM pg_advisory_xact_lock(75077, hashtext(NEW.source || ':' || NEW.ticker || ':' || NEW.condition));
    END IF;
    IF EXISTS (
        SELECT 1 FROM alert_purge_blocks b WHERE b.source = NEW.source AND (
            b.identity = COALESCE(NULLIF(NEW.source_key, ''), 'row:' || NEW.id::text)
            OR (b.legacy_identity AND b.ticker=NEW.ticker AND b.condition=NEW.condition AND b.threshold=NEW.threshold)
            OR (NEW.source='journal_sync' AND b.ticker=NEW.ticker AND b.condition=NEW.condition
                AND ABS(b.threshold-NEW.threshold) <= LEAST(GREATEST(ABS(b.threshold)*0.01,0.05),25))
        )
    ) THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS alert_purge_recreation ON price_alerts;
CREATE TRIGGER alert_purge_recreation BEFORE INSERT OR UPDATE OF source, source_key ON price_alerts
    FOR EACH ROW EXECUTE FUNCTION prevent_purged_alert_recreation();
