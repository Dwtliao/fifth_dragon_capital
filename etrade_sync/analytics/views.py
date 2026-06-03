from etrade_sync.db import get_connection

MATERIALIZED_VIEWS = [
    "mv_unrealized_pnl",
]


def refresh_views():
    """Refresh all materialized views. Called after each sync."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for view in MATERIALIZED_VIEWS:
                cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
                print(f"  refreshed: {view}")
