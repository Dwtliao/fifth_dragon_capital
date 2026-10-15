-- Retire P10 saved Watch Levels only. Apply after deploying position-only readers/writers.
-- Keep key_levels position IDs, stops, notes and all alert/journal/control/history tables.
-- No CASCADE: an unexpected dependency must abort instead of being removed implicitly.
-- Run inside one transaction; the isolated verifier proves rollback and repeatability.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
LOCK TABLE key_levels IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM key_levels k WHERE section='positions' AND
        (to_jsonb(k)->>'support' IS NOT NULL OR to_jsonb(k)->>'resistance' IS NOT NULL
         OR to_jsonb(k)->>'alert_above' IS NOT NULL)
    ) THEN
        RAISE EXCEPTION 'Position metadata contains watch-only values; review before migration';
    END IF;
END;
$$;
DELETE FROM key_levels WHERE section='watch';
ALTER TABLE key_levels DROP CONSTRAINT IF EXISTS key_levels_section_check;
ALTER TABLE key_levels ADD CONSTRAINT key_levels_section_check CHECK (section='positions');
ALTER TABLE key_levels DROP COLUMN IF EXISTS support;
ALTER TABLE key_levels DROP COLUMN IF EXISTS resistance;
ALTER TABLE key_levels DROP COLUMN IF EXISTS alert_above;
COMMIT;
