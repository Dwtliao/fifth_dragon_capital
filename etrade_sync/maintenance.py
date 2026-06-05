from etrade_sync.db import get_connection
from etrade_sync.transaction_identity import build_transaction_fingerprints


def _infer_source_system(transaction_id):
    if isinstance(transaction_id, str) and transaction_id.startswith("CSV:"):
        return "csv"
    return "api"


def _build_source_payload(raw, source_system, row):
    if isinstance(raw, dict):
        if source_system == "csv" and isinstance(raw.get("row"), dict):
            return raw["row"]
        return raw

    return {
        "account_id_key": row["account_id_key"],
        "transaction_id": row["transaction_id"],
        "transaction_date": row["transaction_date"],
        "settlement_date": row["settlement_date"],
        "transaction_type": row["transaction_type"],
        "description": row["description"],
        "description2": row["description2"],
        "amount": row["amount"],
        "symbol": row["symbol"],
        "quantity": row["quantity"],
        "price": row["price"],
        "fee": row["fee"],
    }


def backfill_transaction_provenance(dry_run=False):
    """
    Populate provenance columns for legacy transactions.

    Existing rows created before the provenance layer may have source_system='api'
    by default and no fingerprints at all. This backfill infers the source, rebuilds
    the normalized fingerprints, and makes the ledger rebuild deterministic.
    """
    select_sql = """
        SELECT id, account_id_key, transaction_id, transaction_date, settlement_date,
               transaction_type, description, description2, amount, symbol, quantity,
               price, fee, raw, source_system, source_record_key, source_payload_hash,
               canonical_fingerprint, dedupe_signature
        FROM transactions
        WHERE source_payload_hash IS NULL
           OR canonical_fingerprint IS NULL
           OR dedupe_signature IS NULL
           OR source_system IS NULL
           OR source_record_key IS NULL
           OR (transaction_id LIKE 'CSV:%%' AND source_system <> 'csv')
           OR (transaction_id NOT LIKE 'CSV:%%' AND source_system <> 'api')
        ORDER BY id
    """

    update_sql = """
        UPDATE transactions
        SET source_system = %s,
            source_record_key = %s,
            source_payload_hash = %s,
            canonical_fingerprint = %s,
            dedupe_signature = %s
        WHERE id = %s
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(select_sql)
            rows = cur.fetchall()

            updated = 0
            for row in rows:
                (
                    row_id,
                    account_id_key,
                    transaction_id,
                    transaction_date,
                    settlement_date,
                    transaction_type,
                    description,
                    description2,
                    amount,
                    symbol,
                    quantity,
                    price,
                    fee,
                    raw,
                    source_system,
                    source_record_key,
                    source_payload_hash,
                    canonical_fingerprint,
                    dedupe_signature,
                ) = row

                inferred_source = _infer_source_system(transaction_id)
                payload = _build_source_payload(raw, inferred_source, {
                    "account_id_key": account_id_key,
                    "transaction_id": transaction_id,
                    "transaction_date": transaction_date,
                    "settlement_date": settlement_date,
                    "transaction_type": transaction_type,
                    "description": description,
                    "description2": description2,
                    "amount": amount,
                    "symbol": symbol,
                    "quantity": quantity,
                    "price": price,
                    "fee": fee,
                })
                drop_keys = ["transactionId"] if inferred_source == "api" else None
                identity = build_transaction_fingerprints(
                    account_id_key=account_id_key,
                    transaction_type=transaction_type,
                    amount=amount,
                    symbol=symbol,
                    transaction_date=transaction_date,
                    settlement_date=settlement_date,
                    quantity=quantity,
                    price=price,
                    fee=fee or 0,
                    description=description,
                    description2=description2,
                    source_payload=payload,
                    source_payload_drop_keys=drop_keys,
                )

                if inferred_source == "csv":
                    if isinstance(raw, dict) and raw.get("file_hash") and raw.get("row_number"):
                        inferred_source_record_key = f"{raw['file_hash']}:{raw['row_number']}"
                    else:
                        inferred_source_record_key = source_record_key or transaction_id
                else:
                    inferred_source_record_key = source_record_key or transaction_id

                if dry_run:
                    updated += 1
                    continue

                cur.execute(
                    update_sql,
                    (
                        inferred_source,
                        inferred_source_record_key,
                        identity["source_payload_hash"],
                        identity["canonical_fingerprint"],
                        identity["dedupe_signature"],
                        row_id,
                    ),
                )
                updated += 1

    return {"updated": updated, "dry_run": dry_run}


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
