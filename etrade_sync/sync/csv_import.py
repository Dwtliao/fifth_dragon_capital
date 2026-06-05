"""
Import E*TRADE CSV transaction exports into the transactions table.

E*TRADE web UI export format (Activity/Trade Date, Transaction Date,
Settlement Date, Activity Type, Description, Symbol, Cusip, Quantity #,
Price $, Amount $, Commission, Category, Note).

Synthetic transaction_ids are prefixed with "CSV:" and keyed on a hash
of the business fields, making re-imports safe (idempotent upsert).
"""
import csv
import hashlib
import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from etrade_sync.db import get_connection

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


def _synthetic_id(account_id_key, txn_date, txn_type, symbol, quantity, price, description):
    """Deterministic transaction_id for CSV rows (no API ID available)."""
    key = "|".join([
        account_id_key,
        str(txn_date),
        str(txn_type),
        str(symbol),
        str(quantity),
        str(price),
        str(description)[:80],
    ])
    return "CSV:" + hashlib.sha1(key.encode()).hexdigest()


def import_csv(file_path: str, account_id_key: str) -> dict:
    """
    Parse an E*TRADE CSV export and upsert rows into transactions.

    Returns {"inserted": int, "skipped": int, "errors": list}.
    """
    path = Path(file_path)
    if not path.exists():
        return {"inserted": 0, "skipped": 0, "errors": [f"File not found: {file_path}"]}

    raw_text = path.read_text(encoding="utf-8-sig")  # handle optional BOM

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
            for row in reader:
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

                    txn_id = _synthetic_id(
                        account_id_key, txn_date, txn_type,
                        symbol, quantity, price, description
                    )

                    raw_payload = {
                        "source": "csv_import",
                        "file": path.name,
                        "row": dict(row),
                    }

                    cur.execute("""
                        INSERT INTO transactions
                            (account_id_key, transaction_id, transaction_date,
                             settlement_date, transaction_type, description,
                             amount, symbol, quantity, price, fee, raw)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (transaction_id) DO NOTHING
                    """, (
                        account_id_key, txn_id, txn_date,
                        settlement_date, txn_type, description,
                        amount, symbol, quantity, price, fee,
                        json.dumps(raw_payload),
                    ))

                    if cur.rowcount:
                        inserted += 1
                    else:
                        skipped += 1

                except Exception as e:
                    errors.append(f"Row {trade_date_raw}: {e}")

    return {"inserted": inserted, "skipped": skipped, "errors": errors}
