"""
Write and manage manual lot CSV files for historical positions.

Files live in data/manual_lots/ in E*TRADE CSV format so the existing
import_csv parser handles them without a new code path. Committing these
files to git makes manual lots portable — a fresh rebuild just runs
import-csv on the folder like any other CSV backfill.
"""
import csv
from datetime import date, timedelta
from pathlib import Path

MANUAL_LOTS_DIR = Path(__file__).parent.parent.parent / "data" / "manual_lots"

_HEADER = [
    "Activity/Trade Date", "Transaction Date", "Settlement Date",
    "Activity Type", "Description", "Symbol", "Cusip",
    "Quantity #", "Price $", "Amount $", "Commission", "Category", "Note",
]


def manual_lots_path(account_name: str, account_id: str) -> Path:
    safe_name = (account_name or "account").replace(" ", "_")
    return MANUAL_LOTS_DIR / f"manual_{safe_name}_{account_id}.csv"


def _fmt_date(d: date) -> str:
    return d.strftime("%m/%d/%y")


def rewrite_manual_lots_file(account_name: str, account_id: str, lots: list) -> Path:
    """Rewrite the entire manual lots CSV for an account from a list of lot dicts.

    Each dict must have: symbol, buy_date (date or ISO string), quantity, price, note.
    Deletes the file if lots is empty.
    """
    MANUAL_LOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = manual_lots_path(account_name, account_id)

    if not lots:
        path.unlink(missing_ok=True)
        return path

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_HEADER)
        for lot in lots:
            buy_date = lot["buy_date"]
            if isinstance(buy_date, str):
                buy_date = date.fromisoformat(buy_date)
            settlement = buy_date + timedelta(days=2)
            amount = -(lot["quantity"] * lot["price"])
            w.writerow([
                _fmt_date(buy_date),
                _fmt_date(buy_date),
                _fmt_date(settlement),
                "Bought",
                "Manual lot entry",
                str(lot["symbol"]).upper().strip(),
                "",
                lot["quantity"],
                lot["price"],
                f"{amount:.2f}",
                "0",
                "",
                str(lot.get("note") or "").strip(),
            ])

    return path


def append_manual_lot(
    account_name: str,
    account_id: str,
    symbol: str,
    buy_date: date,
    quantity: float,
    price: float,
    note: str = "",
) -> Path:
    """Append one lot to the account's manual lots CSV. Creates header if the file is new."""
    MANUAL_LOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = manual_lots_path(account_name, account_id)

    needs_header = not path.exists() or path.stat().st_size == 0
    settlement = buy_date + timedelta(days=2)
    amount = -(quantity * price)

    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if needs_header:
            w.writerow(_HEADER)
        w.writerow([
            _fmt_date(buy_date),         # Activity/Trade Date
            _fmt_date(buy_date),         # Transaction Date
            _fmt_date(settlement),       # Settlement Date
            "Bought",                    # Activity Type
            "Manual lot entry",          # Description
            symbol.upper().strip(),      # Symbol
            "",                          # Cusip
            quantity,                    # Quantity #
            price,                       # Price $
            f"{amount:.2f}",             # Amount $
            "0",                         # Commission
            "",                          # Category
            note.strip(),                # Note
        ])

    return path
