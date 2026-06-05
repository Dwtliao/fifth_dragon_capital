"""
Import E*TRADE CSV transaction exports into the transactions table.

E*TRADE web UI export format (Activity/Trade Date, Transaction Date,
Settlement Date, Activity Type, Description, Symbol, Cusip, Quantity #,
Price $, Amount $, Commission, Category, Note).

Synthetic transaction_ids preserve the legacy CSV hash scheme so existing
rows are updated in place, while the provenance columns carry the normalized
source fingerprint for dedupe and audit.
"""
import csv
import hashlib
import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from etrade_sync.db import get_connection
from etrade_sync.transaction_identity import (
    build_legacy_csv_transaction_id,
    build_transaction_fingerprints,
    record_ingest_audit,
)

# Map CSV Activity Type → E*TRADE API transaction_type (same names, but
# verify any edge cases here).
_TYPE_MAP = {
    "Bought":             "Bought",
    "Sold":               "Sold",
    "Dividend":           "Dividend",
    "Qualified Dividend": "Qualified Dividend",
    "Interest Income":    "Interest Income",
    "Fee":                "Fee",
    "Transfer":           "Transfer",
    "Online Transfer":    "Online Transfer",
    "Stock Split":        "Stock Split",
    "Redemption":         "Redemption",
    "Exchange":           "Exchange",
}


def _parse_date(s):
    """Parse MM/DD/YY → timezone-aware datetime.

    Use noon UTC so that ::date casts in PostgreSQL land on the correct
    calendar date regardless of the server's local timezone offset.
    """
    if not s or s.strip() == "--":
        return None
    s = s.strip()
    dt = datetime.strptime(s, "%m/%d/%y")
    return dt.replace(hour=12, tzinfo=timezone.utc)


def _decimal_or_none(s):
    if not s or s.strip() in ("", "--"):
        return None
    try:
        return float(s.strip())
    except ValueError:
        return None


def _resolve_csv_transaction_id(cur, account_id_key, legacy_txn_id, source_payload_hash):
    """
    Preserve compatibility with the legacy CSV ID scheme.

    Prefer the historical transaction_id if it exists; fall back to any
    previously imported CSV row that already carries the same payload hash.
    """
    cur.execute(
        """
        SELECT transaction_id
        FROM transactions
        WHERE account_id_key = %s
          AND transaction_id = %s
        LIMIT 1
        """,
        (account_id_key, legacy_txn_id),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute(
        """
        SELECT transaction_id
        FROM transactions
        WHERE account_id_key = %s
          AND source_system = 'csv'
          AND source_payload_hash = %s
        ORDER BY created_at, id
        LIMIT 1
        """,
        (account_id_key, source_payload_hash),
    )
    row = cur.fetchone()
    return row[0] if row else legacy_txn_id


def import_csv(file_path: str, account_id_key: str) -> dict:
    """
    Parse an E*TRADE CSV export and upsert rows into transactions.

    Returns {"inserted": int, "skipped": int, "errors": list}.
    """
    path = Path(file_path)
    if not path.exists():
        return {"inserted": 0, "skipped": 0, "errors": [f"File not found: {file_path}"]}

    raw_text = path.read_text(encoding="utf-8-sig")  # handle optional BOM
    file_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    # The CSV has a multi-line preamble before the header row.
    # Find the header line that starts with "Activity/Trade Date".
    lines = raw_text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Activity/Trade Date"):
            header_idx = i
            break

    if header_idx is None:
        return {"inserted": 0, "skipped": 0, "errors": ["Could not find header row in CSV"]}

    data_text = "\n".join(lines[header_idx:])
    reader = csv.DictReader(StringIO(data_text))

    inserted = skipped = 0
    errors = []

    with get_connection() as conn:
        with conn.cursor() as cur:
            for row_number, row in enumerate(reader, start=1):
                # Stop at footer lines (non-data rows start with quotes or blanks)
                trade_date_raw = row.get("Activity/Trade Date", "").strip()
                if not trade_date_raw or not trade_date_raw[0].isdigit():
                    continue

                try:
                    txn_date        = _parse_date(trade_date_raw)
                    settlement_date = _parse_date(row.get("Settlement Date", ""))
                    txn_type        = _TYPE_MAP.get(
                        row.get("Activity Type", "").strip(),
                        row.get("Activity Type", "").strip()
                    )
                    description     = row.get("Description", "").strip() or None
                    symbol_raw      = row.get("Symbol", "").strip()
                    symbol          = symbol_raw if symbol_raw and symbol_raw != "--" else None
                    quantity        = _decimal_or_none(row.get("Quantity #"))
                    price           = _decimal_or_none(row.get("Price $"))
                    amount          = _decimal_or_none(row.get("Amount $"))
                    fee             = _decimal_or_none(row.get("Commission")) or 0

                    identity = build_transaction_fingerprints(
                        account_id_key=account_id_key,
                        transaction_type=txn_type,
                        amount=amount,
                        symbol=symbol,
                        transaction_date=txn_date,
                        settlement_date=settlement_date,
                        quantity=quantity,
                        price=price,
                        fee=fee,
                        description=description,
                        description2=row.get("Note", "").strip() or None,
                        source_payload=row,
                        source_payload_drop_keys=None,
                    )
                    legacy_txn_id = build_legacy_csv_transaction_id(
                        account_id_key,
                        txn_date,
                        txn_type,
                        symbol,
                        quantity,
                        price,
                        description,
                    )
                    source_record_key = f"{file_hash}:{row_number}"

                    raw_payload = {
                        "source": "csv_import",
                        "file": path.name,
                        "file_hash": file_hash,
                        "row_number": row_number,
                        "row": dict(row),
                    }

                    txn_id = _resolve_csv_transaction_id(
                        cur,
                        account_id_key,
                        legacy_txn_id,
                        identity["source_payload_hash"],
                    )

                    cur.execute("""
                        INSERT INTO transactions
                            (account_id_key, transaction_id, transaction_date,
                             settlement_date, transaction_type, description,
                             description2, amount, symbol, quantity, price, fee,
                             source_system, source_record_key, source_payload_hash,
                             canonical_fingerprint, dedupe_signature, raw)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (transaction_id) DO UPDATE SET
                            transaction_date     = EXCLUDED.transaction_date,
                            settlement_date     = EXCLUDED.settlement_date,
                            transaction_type    = EXCLUDED.transaction_type,
                            description         = EXCLUDED.description,
                            description2        = EXCLUDED.description2,
                            amount              = EXCLUDED.amount,
                            symbol              = COALESCE(EXCLUDED.symbol, transactions.symbol),
                            quantity            = EXCLUDED.quantity,
                            price               = EXCLUDED.price,
                            fee                 = EXCLUDED.fee,
                            source_system       = EXCLUDED.source_system,
                            source_record_key   = EXCLUDED.source_record_key,
                            source_payload_hash = EXCLUDED.source_payload_hash,
                            canonical_fingerprint = EXCLUDED.canonical_fingerprint,
                            dedupe_signature    = EXCLUDED.dedupe_signature,
                            raw                 = EXCLUDED.raw
                        RETURNING (xmax = 0) AS was_inserted
                    """, (
                        account_id_key, txn_id, txn_date,
                        settlement_date, txn_type, description,
                        row.get("Note", "").strip() or None,
                        amount, symbol, quantity, price, fee,
                        "csv", source_record_key, identity["source_payload_hash"],
                        identity["canonical_fingerprint"], identity["dedupe_signature"],
                        json.dumps(raw_payload),
                    ))

                    was_inserted = cur.fetchone()[0]
                    if was_inserted:
                        inserted += 1
                    else:
                        skipped += 1

                    record_ingest_audit(
                        cur,
                        account_id_key=account_id_key,
                        transaction_id=txn_id,
                        source_system="csv",
                        source_record_key=source_record_key,
                        source_payload_hash=identity["source_payload_hash"],
                        canonical_fingerprint=identity["canonical_fingerprint"],
                        dedupe_signature=identity["dedupe_signature"],
                        write_status="upserted",
                        raw_payload=raw_payload,
                    )

                except Exception as e:
                    errors.append(f"Row {trade_date_raw}: {e}")

    return {"inserted": inserted, "skipped": skipped, "errors": errors}
