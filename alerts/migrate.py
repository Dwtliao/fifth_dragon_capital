"""Apply only the alert-controls migration: python -m alerts.migrate."""
from pathlib import Path
from alerts.lifecycle import LOCK_NAMESPACE, POLL_LOCK
from etrade_sync.db import get_connection


def main():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_xact_lock(%s,%s)', (LOCK_NAMESPACE, POLL_LOCK))
            if not cur.fetchone()[0]:
                raise RuntimeError('An alert poll is running; retry migration when it finishes')
            cur.execute((Path(__file__).resolve().parent.parent / 'data_model/075_alert_controls.sql').read_text())
        conn.commit()
        print('Applied 075_alert_controls.sql (existing alert rows unchanged).')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
