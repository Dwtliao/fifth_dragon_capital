"""Targeted rebuild of just the portfolio-timeseries view chain (issue #67).

Drops and recreates only mv_portfolio_timeseries, mv_portfolio_timeseries_by_account,
mv_benchmark_comparison, and mv_benchmark_comparison_by_account — the views whose SQL
changed to union in `portfolio_value_backfill`. Deliberately does NOT use
`etrade_sync.db.migrate()`, which drops and recreates every materialized view in the
project for what should be a scoped, 4-view change.

Run after portfolio_value_backfill has been populated (see backfill_portfolio_history.py)
and after any further edits to data_model/052, 055, 056, or 058.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from etrade_sync.db import get_connection

DATA_MODEL_DIR = Path(__file__).parent.parent / "data_model"
BACKFILL_TABLE_SQL = "051b_portfolio_value_backfill.sql"

# Dependency order: 058/055 each depend on 056/052 respectively.
DROP_ORDER = [
    "mv_benchmark_comparison_by_account",
    "mv_benchmark_comparison",
    "mv_portfolio_timeseries_by_account",
    "mv_portfolio_timeseries",
]
SQL_FILES_IN_CREATE_ORDER = [
    "052_mv_portfolio_timeseries.sql",
    "056_mv_portfolio_timeseries_by_account.sql",
    "055_mv_benchmark_comparison.sql",
    "058_mv_benchmark_comparison_by_account.sql",
]
REFRESH_ORDER = [
    "mv_portfolio_timeseries",
    "mv_portfolio_timeseries_by_account",
    "mv_benchmark_comparison",
    "mv_benchmark_comparison_by_account",
]


def rebuild():
    conn = get_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Must exist before the views below are recreated — they reference
            # it directly, and CREATE MATERIALIZED VIEW IF NOT EXISTS still
            # parses/analyzes the query body even when the view already
            # exists, so table-not-found errors surface regardless of order.
            cur.execute((DATA_MODEL_DIR / BACKFILL_TABLE_SQL).read_text())
            print(f"  ensured table exists: {BACKFILL_TABLE_SQL}")

            for view in DROP_ORDER:
                cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {view}")
                print(f"  dropped: {view}")

            for filename in SQL_FILES_IN_CREATE_ORDER:
                cur.execute((DATA_MODEL_DIR / filename).read_text())
                print(f"  recreated from: {filename}")

            for view in REFRESH_ORDER:
                cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
                print(f"  refreshed: {view}")
    finally:
        conn.close()

    print("Portfolio view rebuild complete.")


if __name__ == "__main__":
    rebuild()
