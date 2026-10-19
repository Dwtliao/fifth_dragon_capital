import os
from pathlib import Path

import psycopg2
from etrade_sync.config import DATABASE_URL

DATA_MODEL_DIR = Path(__file__).parent.parent / "data_model"

# Drop in reverse dependency order so CASCADE isn't needed.
_MATERIALIZED_VIEWS = [
    "mv_benchmark_comparison_by_account",  # depends on mv_portfolio_timeseries_by_account
    "mv_benchmark_comparison",
    "mv_attribution_timeseries",
    "mv_allocations",
    "mv_portfolio_timeseries_by_account",
    "mv_portfolio_timeseries",
    "mv_unrealized_pnl",
]


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def _execute_schema_file(cur, path):
    """Do not replay the legacy expiry backfill after the column is installed."""
    contents = path.read_text()
    if path.name == '069_price_alerts_source_tier_expiry.sql':
        cur.execute("""SELECT EXISTS(SELECT 1 FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='price_alerts'
            AND column_name='expires_at')""")
        if cur.fetchone()[0]:
            # Retain additive schema repair, but preserve explicit no-expiry rules.
            before, marker, _ = contents.partition('-- Back-fill:')
            if not marker:
                raise RuntimeError(
                    "069_price_alerts_source_tier_expiry.sql no longer contains the "
                    "'-- Back-fill:' marker; refusing to risk replaying the expiry "
                    "backfill against explicit no-expiry alerts. Update this guard.")
            contents = before
    cur.execute(contents)


def create_tables():
    """Execute all data_model/*.sql files in numbered order."""
    sql_files = sorted(DATA_MODEL_DIR.glob("*.sql"))
    with get_connection() as conn:
        with conn.cursor() as cur:
            for path in sql_files:
                _execute_schema_file(cur, path)


def migrate():
    """Apply schema changes: drop all materialized views, re-run all SQL files, refresh views.

    Safe to run after any data_model/*.sql change. Table data is preserved —
    only materialized views are dropped and recreated.
    """
    sql_files = sorted(DATA_MODEL_DIR.glob("*.sql"))

    # Step 1: drop views (autocommit — DDL outside transaction)
    conn = get_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for view in _MATERIALIZED_VIEWS:
                cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {view}")
                print(f"  dropped: {view}")
    finally:
        conn.close()

    # Step 2: recreate tables + views
    with get_connection() as conn:
        with conn.cursor() as cur:
            for path in sql_files:
                _execute_schema_file(cur, path)
                print(f"  applied: {path.name}")

    # Step 3: refresh views with data
    conn = get_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for view in reversed(_MATERIALIZED_VIEWS):
                cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
                print(f"  refreshed: {view}")
    finally:
        conn.close()

    print("Migration complete.")
