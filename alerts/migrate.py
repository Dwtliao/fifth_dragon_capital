"""Apply only the alert-controls migration: python -m alerts.migrate."""
from pathlib import Path
import argparse
from alerts.lifecycle import LOCK_NAMESPACE, POLL_LOCK
from etrade_sync.db import get_connection


def main(purge=False):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_xact_lock(%s,%s)', (LOCK_NAMESPACE, POLL_LOCK))
            if not cur.fetchone()[0]:
                raise RuntimeError('An alert poll is running; retry migration when it finishes')
            filename = '076_alert_purge.sql' if purge else '075_alert_controls.sql'
            cur.execute((Path(__file__).resolve().parent.parent / 'data_model' / filename).read_text())
        conn.commit()
        print(f'Applied {filename} (existing alert rows unchanged).')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Apply a targeted alert migration')
    parser.add_argument('--purge', action='store_true', help='Apply 076 purge support instead of 075 controls')
    main(purge=parser.parse_args().purge)
