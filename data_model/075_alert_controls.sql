-- Additive, targeted migration. Do not rerun older data-model backfills.
CREATE TABLE IF NOT EXISTS alert_controls (
    source TEXT NOT NULL,
    identity TEXT NOT NULL,
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    snoozed_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, identity)
);

-- Preserve legacy suppression once; never overwrite subsequent operator choices.
INSERT INTO alert_controls (source, identity, paused)
SELECT source, COALESCE(NULLIF(source_key, ''), 'row:' || id::text), TRUE
FROM price_alerts WHERE NOT enabled OR archived_at IS NOT NULL
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS alert_action_history (
    id BIGSERIAL PRIMARY KEY,
    alert_id INTEGER,
    source TEXT NOT NULL,
    identity TEXT NOT NULL,
    action TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Preserve operator intent when journal promotion/legacy backfill changes identity.
CREATE OR REPLACE FUNCTION preserve_alert_controls_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.source IS DISTINCT FROM NEW.source OR OLD.source_key IS DISTINCT FROM NEW.source_key THEN
        INSERT INTO alert_controls (source, identity, paused, snoozed_until)
        SELECT NEW.source, COALESCE(NULLIF(NEW.source_key, ''), 'row:' || NEW.id::text), paused, snoozed_until
        FROM alert_controls
        WHERE source = OLD.source AND identity = COALESCE(NULLIF(OLD.source_key, ''), 'row:' || OLD.id::text)
        ON CONFLICT (source, identity) DO UPDATE
        SET paused = alert_controls.paused OR EXCLUDED.paused,
            snoozed_until = GREATEST(alert_controls.snoozed_until, EXCLUDED.snoozed_until),
            updated_at = NOW();
        INSERT INTO alert_action_history (alert_id, source, identity, action, details)
        VALUES (NEW.id, NEW.source, COALESCE(NULLIF(NEW.source_key, ''), 'row:' || NEW.id::text),
            'identity_changed', jsonb_build_object('old_source', OLD.source,
                'old_identity', COALESCE(NULLIF(OLD.source_key, ''), 'row:' || OLD.id::text)));
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS alert_controls_identity ON price_alerts;
CREATE TRIGGER alert_controls_identity AFTER UPDATE OF source, source_key ON price_alerts
    FOR EACH ROW EXECUTE FUNCTION preserve_alert_controls_identity();

CREATE TABLE IF NOT EXISTS alert_notification_events (
    id BIGSERIAL PRIMARY KEY,
    alert_id INTEGER NOT NULL REFERENCES price_alerts(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    label TEXT,
    condition TEXT NOT NULL,
    threshold NUMERIC NOT NULL,
    price NUMERIC NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('pending', 'sent', 'failed', 'not_configured', 'unknown', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    next_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS alert_notification_events_alert_idx
    ON alert_notification_events (alert_id, id DESC);

CREATE TABLE IF NOT EXISTS alert_poll_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    summary JSONB NOT NULL DEFAULT '{}',
    error TEXT
);
