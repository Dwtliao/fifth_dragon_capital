from etrade_sync.db import get_connection


_DUPLICATE_GROUPS_SQL = """
    SELECT
        COUNT(*) AS duplicate_groups,
        COALESCE(SUM(group_size - 1), 0) AS rows_to_delete
    FROM (
        SELECT COUNT(*) AS group_size
        FROM transaction_ingest_audit
        WHERE source_record_key IS NOT NULL
        GROUP BY account_id_key, source_system, source_record_key
        HAVING COUNT(*) > 1
    ) groups
"""

_UPDATE_KEEPERS_SQL = """
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
      AND ranked.rn = 1
"""

_DELETE_DUPES_SQL = """
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
      AND ranked.rn > 1
"""


def cleanup_transaction_ingest_audit(dry_run=False):
    """
    Collapse duplicate transaction_ingest_audit rows keyed by account/source/source_record_key.

    Returns a summary dict with duplicate group and row counts. Use dry_run=True to
    inspect the blast radius without mutating data.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_DUPLICATE_GROUPS_SQL)
            duplicate_groups, rows_to_delete = cur.fetchone()

            result = {
                "duplicate_groups": duplicate_groups,
                "rows_to_delete": rows_to_delete,
                "keepers_updated": 0,
                "rows_deleted": 0,
                "dry_run": dry_run,
            }

            if dry_run or rows_to_delete == 0:
                return result

            cur.execute(_UPDATE_KEEPERS_SQL)
            result["keepers_updated"] = cur.rowcount

            cur.execute(_DELETE_DUPES_SQL)
            result["rows_deleted"] = cur.rowcount

    return result
