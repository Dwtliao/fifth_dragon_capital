import json
import os
import time

from etrade_sync.db import get_connection

_TRIGGERED_BY = os.environ.get("SYNC_TRIGGERED_BY", "manual")


def start_run(job_name):
    """Insert a 'running' row and return its id."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_log (job_name, status, triggered_by)
                VALUES (%s, 'running', %s)
                RETURNING id
                """,
                (job_name, _TRIGGERED_BY),
            )
            return cur.fetchone()[0]


def finish_run(log_id, status, rows_synced=None, error_msg=None):
    """Close out a run row with final status, duration, and row counts."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sync_log SET
                    finished_at = NOW(),
                    status      = %s,
                    duration_s  = EXTRACT(EPOCH FROM (NOW() - started_at)),
                    rows_synced = %s,
                    error_msg   = %s
                WHERE id = %s
                """,
                (
                    status,
                    json.dumps(rows_synced) if rows_synced else None,
                    error_msg,
                    log_id,
                ),
            )


def log_token_stale(job_name):
    """Single-call helper for shell scripts to record a token_stale event."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_log (job_name, finished_at, status, duration_s, triggered_by)
                VALUES (%s, NOW(), 'token_stale', 0, %s)
                """,
                (job_name, _TRIGGERED_BY),
            )
