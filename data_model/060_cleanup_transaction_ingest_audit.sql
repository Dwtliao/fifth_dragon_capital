-- Cleanup pass for historical duplicate ingest audit rows.
-- Collapses repeated rows for the same account/source/source_record_key
-- into one canonical audit row and enforces uniqueness going forward.

WITH ranked AS (
    SELECT
        id,
        account_id_key,
        source_system,
        source_record_key,
        ROW_NUMBER() OVER (
            PARTITION BY account_id_key, source_system, source_record_key
            ORDER BY COALESCE(last_seen_at, created_at) DESC, created_at DESC, id DESC
        ) AS rn,
        SUM(COALESCE(observed_count, 1)) OVER (
            PARTITION BY account_id_key, source_system, source_record_key
        ) AS observed_total,
        MAX(COALESCE(last_seen_at, created_at)) OVER (
            PARTITION BY account_id_key, source_system, source_record_key
        ) AS last_seen_total
    FROM transaction_ingest_audit
    WHERE source_record_key IS NOT NULL
)
UPDATE transaction_ingest_audit audit
SET observed_count = ranked.observed_total,
    last_seen_at = ranked.last_seen_total
FROM ranked
WHERE audit.id = ranked.id
  AND ranked.rn = 1;

WITH ranked AS (
    SELECT
        id,
        account_id_key,
        source_system,
        source_record_key,
        ROW_NUMBER() OVER (
            PARTITION BY account_id_key, source_system, source_record_key
            ORDER BY COALESCE(last_seen_at, created_at) DESC, created_at DESC, id DESC
        ) AS rn
    FROM transaction_ingest_audit
    WHERE source_record_key IS NOT NULL
)
DELETE FROM transaction_ingest_audit audit
USING ranked
WHERE audit.id = ranked.id
  AND ranked.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS transaction_ingest_audit_source_row_unique
    ON transaction_ingest_audit (account_id_key, source_system, source_record_key);
