from etrade_sync.db import get_connection

MATERIALIZED_VIEWS = [
    "mv_unrealized_pnl",
    "mv_portfolio_timeseries",
    "mv_portfolio_timeseries_by_account",
    "mv_allocations",
    "mv_attribution_timeseries",
    "mv_benchmark_comparison",
    "mv_benchmark_comparison_by_account",  # depends on mv_portfolio_timeseries_by_account
]


def refresh_views():
    """Refresh all materialized views. Called after each sync.

    CONCURRENTLY requires autocommit mode — cannot run inside a transaction.
    """
    conn = get_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for view in MATERIALIZED_VIEWS:
                cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
                print(f"  refreshed: {view}")
    finally:
        conn.close()
