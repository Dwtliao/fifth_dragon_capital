"""Operator controls survive source reconciliation and managed-row recreation."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math

from etrade_sync.db import get_connection

LOCK_NAMESPACE = 75075
POLL_LOCK = 0


def identity(alert):
    return alert.get('source_key') or f"row:{alert['id']}"


def eligibility_sql(alias='a', controls='c'):
    return (f"{alias}.enabled AND {alias}.archived_at IS NULL "
            f"AND ({alias}.expires_at IS NULL OR {alias}.expires_at > NOW()) "
            f"AND NOT COALESCE({controls}.paused, FALSE) "
            f"AND ({controls}.snoozed_until IS NULL OR {controls}.snoozed_until <= NOW())")


def state(alert, now=None):
    now = now or datetime.now(timezone.utc)
    if alert.get('archived_at'):
        return 'Archived'
    if alert.get('expires_at') and alert['expires_at'] <= now:
        return 'Expired'
    if alert.get('paused'):
        return 'Paused'
    if alert.get('snoozed_until') and alert['snoozed_until'] > now:
        return 'Snoozed'
    if not alert.get('enabled', True):
        return 'Disabled'
    return 'Condition met' if alert.get('triggered') else 'Armed'


def is_eligible(alert, now=None):
    return state(alert, now) in ('Armed', 'Condition met')


def valid_price(price):
    try:
        return math.isfinite(float(price)) and float(price) > 0
    except (ValueError, TypeError):
        return False


@contextmanager
def alert_lock(conn, alert_id):
    """Session lock shared by lifecycle actions and delivery; no open network transaction."""
    with conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_lock(%s, %s)', (LOCK_NAMESPACE, alert_id))
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_unlock(%s, %s)', (LOCK_NAMESPACE, alert_id))
        conn.commit()


def load_alerts(conn, eligible_only=False, alert_id=None):
    clauses, params = [], []
    if eligible_only:
        clauses.append(eligibility_sql())
    if alert_id is not None:
        clauses.append('a.id = %s')
        params.append(alert_id)
    where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT a.*, a.threshold::float AS threshold,
                   COALESCE(c.paused, FALSE) AS paused, c.snoozed_until,
                   e.status AS delivery_status, e.error AS delivery_error,
                   e.attempts AS delivery_attempts
            FROM price_alerts a
            LEFT JOIN alert_controls c ON c.source = a.source
                AND c.identity = COALESCE(NULLIF(a.source_key, ''), 'row:' || a.id::text)
            LEFT JOIN LATERAL (SELECT status, error, attempts
                FROM alert_notification_events WHERE alert_id = a.id
                ORDER BY id DESC LIMIT 1) e ON TRUE
            {where} ORDER BY a.id
        """, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def control_alert(alert_id, action, snoozed_until=None, threshold=None):
    if action not in ('pause', 'snooze', 'resume', 'archive', 'threshold', 'rearm'):
        raise ValueError('Unsupported alert action')
    if action == 'snooze' and (snoozed_until is None or snoozed_until.tzinfo is None
            or snoozed_until <= datetime.now(timezone.utc)):
        raise ValueError('Snooze must end at a future timezone-aware time')
    if action == 'threshold' and not valid_price(threshold):
        raise ValueError('Threshold must be finite and greater than zero')
    conn = get_connection()
    try:
        with alert_lock(conn, alert_id):
            # Lock the source row while recording operator intent. If a compiler
            # changes identity afterward, the migration trigger transfers controls.
            with conn.cursor() as cur:
                cur.execute('SELECT id FROM price_alerts WHERE id=%s FOR UPDATE', (alert_id,))
            rows = load_alerts(conn, alert_id=alert_id)
            if not rows:
                raise ValueError('Alert no longer exists')
            alert = rows[0]
            if action in ('threshold', 'archive') and alert['source'] != 'manual':
                raise ValueError('Managed levels must be edited at their source; use Pause instead')
            if action == 'resume':
                if alert.get('expires_at') and alert['expires_at'] <= datetime.now(timezone.utc):
                    raise ValueError('Expired alert cannot resume; update its source or create a new alert')
                if alert['source'] != 'manual' and alert.get('archived_at'):
                    raise ValueError('Refresh the managed source before resuming this archived alert')
            with conn.cursor() as cur:
                if action in ('pause', 'snooze', 'resume', 'archive'):
                    cur.execute("""INSERT INTO alert_controls
                        (source, identity, paused, snoozed_until) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (source, identity) DO UPDATE
                        SET paused = EXCLUDED.paused, snoozed_until = EXCLUDED.snoozed_until,
                            updated_at = NOW()""",
                        (alert['source'], identity(alert), action in ('pause', 'archive'),
                         snoozed_until if action == 'snooze' else None))
                if action == 'resume':
                    cur.execute('UPDATE price_alerts SET enabled=TRUE, archived_at=NULL, triggered=FALSE WHERE id=%s', (alert_id,))
                elif action == 'archive':
                    cur.execute('UPDATE price_alerts SET enabled=FALSE, archived_at=NOW() WHERE id=%s', (alert_id,))
                elif action == 'threshold':
                    cur.execute('UPDATE price_alerts SET threshold=%s, triggered=FALSE WHERE id=%s', (threshold, alert_id))
                elif action == 'rearm':
                    cur.execute('UPDATE price_alerts SET triggered=FALSE WHERE id=%s', (alert_id,))
                cur.execute("""UPDATE alert_notification_events SET status='cancelled', updated_at=NOW()
                    WHERE alert_id=%s AND status IN ('failed','not_configured','pending','unknown')""", (alert_id,))
                cur.execute("""INSERT INTO alert_action_history
                    (alert_id, source, identity, action, details) VALUES (%s,%s,%s,%s,%s::jsonb)""",
                    (alert_id, alert['source'], identity(alert), action,
                     json.dumps({'old_threshold': alert['threshold'], 'threshold': threshold,
                                 'snoozed_until': snoozed_until.isoformat() if snoozed_until else None})))
            conn.commit()
    finally:
        conn.close()
