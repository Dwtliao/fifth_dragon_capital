"""Validated workspace mutations. No quote requests, notifications, or schema changes."""
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from alerts.lifecycle import (LOCK_NAMESPACE, identity, is_eligible,
                              load_alerts, state, valid_price)
from etrade_sync.db import get_connection


def fingerprint(alert):
    """Ignore polling/delivery changes, but reject changed configuration or controls."""
    fields = ('id', 'source', 'source_key', 'ticker', 'label', 'condition', 'threshold',
              'enabled', 'archived_at', 'expires_at', 'paused', 'snoozed_until')
    return hashlib.sha256(json.dumps({k: alert.get(k) for k in fields},
                                     default=str, sort_keys=True).encode()).hexdigest()


def action_error(alert, action, now=None):
    now = now or datetime.now(timezone.utc)
    if action in ('pause', 'snooze', 'rearm') and not is_eligible(alert, now):
        return 'Requires an active alert'
    if action == 'archive' and (alert['source'] != 'manual' or alert.get('archived_at')):
        return 'Archive is only available for unarchived manual alerts'
    if action == 'resume':
        if is_eligible(alert, now):
            return 'Already active; use explicit Rearm if needed'
        if alert.get('expires_at') and alert['expires_at'] <= now:
            return 'Expired: update its source or create a new alert'
        if alert['source'] != 'manual' and alert.get('archived_at'):
            return 'Refresh the managed source before resuming'
    return None


def preview_action(alerts, action):
    if action not in ('pause', 'snooze', 'resume', 'archive', 'rearm', 'purge'):
        raise ValueError('Unsupported action')
    return [dict(id=a['id'], ticker=a['ticker'], source=a['source'],
                 condition=a['condition'], threshold=a['threshold'], status=state(a),
                 fingerprint=fingerprint(a), error=action_error(a, action)) for a in alerts]


def _audit_json(value):
    if isinstance(value, dict):
        return {k: _audit_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _audit(cur, alert, action, details):
    cur.execute('INSERT INTO alert_action_history (alert_id,source,identity,action,details) '
                'VALUES (%s,%s,%s,%s,%s::jsonb)',
                (alert['id'], alert['source'], identity(alert), action, json.dumps(_audit_json(details), default=str, allow_nan=False)))


def apply_bulk(preview, action, snoozed_until=None):
    """All-or-nothing: reject changed/ineligible targets; never silently skip a subset."""
    if not preview or len({r['id'] for r in preview}) != len(preview):
        raise ValueError('Select one or more distinct alert IDs')
    if action not in ('pause', 'snooze', 'resume', 'archive', 'rearm', 'consolidate', 'purge'):
        raise ValueError('Unsupported action')
    if any(r.get('error') for r in preview):
        raise ValueError('Some selected alerts do not permit this action; adjust selection')
    if action == 'snooze' and (snoozed_until is None or snoozed_until.tzinfo is None or
            snoozed_until <= datetime.now(timezone.utc)):
        raise ValueError('Snooze must end at a future timezone-aware time')
    conn = get_connection()
    try:
        ids = sorted(r['id'] for r in preview)
        expected = {r['id']: r for r in preview}
        with conn.cursor() as cur:
            if action == 'purge':
                guards = sorted({f"{r['source']}:{r['ticker']}:{r['condition']}" for r in preview})
                for guard in guards:
                    cur.execute('SELECT pg_advisory_lock(%s,hashtext(%s))', (LOCK_NAMESPACE + 2, guard))
            for alert_id in ids:
                cur.execute('SELECT pg_advisory_lock(%s,%s)', (LOCK_NAMESPACE, alert_id))
        conn.commit()
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM price_alerts WHERE id=ANY(%s) ORDER BY id FOR UPDATE', (ids,))
        current = []
        for alert_id in ids:
            rows = load_alerts(conn, alert_id=alert_id)
            if not rows or fingerprint(rows[0]) != expected[alert_id]['fingerprint']:
                raise ValueError(f'Alert #{alert_id} changed since preview. Refresh and preview again; nothing changed.')
            error = action_error(rows[0], action) if action != 'consolidate' else None
            if error:
                raise ValueError(f'Alert #{alert_id}: {error}; nothing changed')
            current.append(rows[0])
        if action == 'consolidate' and not consolidation_preview(current):
            raise ValueError('The pair no longer qualifies as an exact structural duplicate; nothing changed')
        with conn.cursor() as cur:
            for alert in current:
                if action == 'purge':
                    cur.execute('SELECT * FROM alert_notification_events WHERE alert_id=%s ORDER BY id', (alert['id'],))
                    columns = [d.name for d in cur.description]
                    events = [dict(zip(columns, row)) for row in cur.fetchall()]
                    _audit(cur, alert, 'purge', {'bulk_ids': ids, 'alert': alert, 'notifications': events})
                    cur.execute('''INSERT INTO alert_purge_blocks
                        (original_alert_id,source,identity,ticker,condition,threshold,legacy_identity)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (original_alert_id) DO NOTHING''',
                        (alert['id'], alert['source'], identity(alert), alert['ticker'],
                         alert['condition'], alert['threshold'], not bool(alert.get('source_key'))))
                    cur.execute('DELETE FROM price_alerts WHERE id=%s', (alert['id'],))
                    continue
                if action == 'consolidate' and alert['source'] == 'key_levels_watch':
                    continue  # Structural row and its notification history are untouched.
                if action != 'rearm':
                    cur.execute('''INSERT INTO alert_controls (source,identity,paused,snoozed_until)
                        VALUES (%s,%s,%s,%s) ON CONFLICT (source,identity) DO UPDATE
                        SET paused=EXCLUDED.paused, snoozed_until=EXCLUDED.snoozed_until, updated_at=NOW()''',
                        (alert['source'], identity(alert), action in ('pause', 'archive', 'consolidate'),
                         snoozed_until if action == 'snooze' else None))
                if action == 'resume':
                    cur.execute('UPDATE price_alerts SET enabled=TRUE, archived_at=NULL, triggered=FALSE WHERE id=%s', (alert['id'],))
                elif action in ('archive', 'consolidate'):
                    cur.execute('UPDATE price_alerts SET enabled=FALSE, archived_at=NOW() WHERE id=%s', (alert['id'],))
                elif action == 'rearm':
                    cur.execute('UPDATE price_alerts SET triggered=FALSE WHERE id=%s', (alert['id'],))
                cur.execute("UPDATE alert_notification_events SET status='cancelled', updated_at=NOW() "
                            "WHERE alert_id=%s AND status IN ('failed','not_configured','pending','unknown')", (alert['id'],))
                _audit(cur, alert, action, {'bulk_ids': ids, 'snoozed_until': snoozed_until})
        conn.commit()
        return ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()  # Releases all session locks, including on validation failure.


def consolidation_preview(alerts):
    """Only one exact manual/journal + structural pair. Near levels are review-only."""
    if len(alerts) != 2 or not all(is_eligible(a) for a in alerts):
        return None
    sources = {a['source'] for a in alerts}
    if sources not in ({'manual', 'key_levels_watch'}, {'journal_sync', 'key_levels_watch'}):
        return None
    if len({(a['ticker'], a['condition'], a['threshold']) for a in alerts}) != 1:
        return None
    return [dict(id=a['id'], ticker=a['ticker'], source=a['source'], threshold=a['threshold'],
                 fingerprint=fingerprint(a), error=None) for a in alerts]


def _manual_values(ticker, label, condition, threshold, expires_at):
    ticker = ticker.strip().upper()
    if not re.fullmatch(r'[A-Z0-9^][A-Z0-9.^=\-]{0,31}', ticker):
        raise ValueError('Use a Yahoo ticker such as XLE, ^VIX, NQ=F, or BTC-USD')
    if condition not in ('above', 'below') or not valid_price(threshold):
        raise ValueError('Choose above/below and a finite threshold greater than zero')
    if expires_at is not None and (expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc)):
        raise ValueError('Expiry must be a future timezone-aware time')
    return ticker, label.strip() or None, condition, float(threshold), expires_at


def save_manual(ticker, label, condition, threshold, expires_at=None, *,
                expected=None, allow_duplicate=False):
    values = _manual_values(ticker, label, condition, threshold, expires_at)
    conn = get_connection()
    try:
        # Serialize creation/edit duplicate checks for this symbol. Pollers never use
        # this namespace; the existing alert lock still serializes edits with SMTP.
        symbol_lock = int.from_bytes(hashlib.sha256(values[0].encode()).digest()[:4], 'big', signed=True)
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_lock(%s,%s)', (LOCK_NAMESPACE + 1, symbol_lock))
            if expected:
                cur.execute('SELECT pg_advisory_lock(%s,%s)', (LOCK_NAMESPACE, expected['id']))
        conn.commit()
        existing = None
        if expected:
            with conn.cursor() as cur:
                cur.execute('SELECT id FROM price_alerts WHERE id=%s FOR UPDATE', (expected['id'],))
            rows = load_alerts(conn, alert_id=expected['id'])
            if not rows or rows[0]['source'] != 'manual' or fingerprint(rows[0]) != expected['fingerprint']:
                raise ValueError('Alert changed or is source-managed. Refresh before editing; nothing changed.')
            existing = rows[0]
        with conn.cursor() as cur:
            cur.execute('''SELECT id FROM price_alerts WHERE ticker=%s AND condition=%s AND threshold=%s
                AND enabled AND archived_at IS NULL AND (expires_at IS NULL OR expires_at>NOW())
                AND id<>%s''', (values[0], values[2], values[3], existing['id'] if existing else -1))
            duplicates = [r[0] for r in cur.fetchall()]
            if duplicates and not allow_duplicate:
                raise ValueError(f'Exact existing level in alert IDs {duplicates}. Confirm an intentional duplicate to save.')
            if existing:
                rearm = (existing['ticker'], existing['condition'], existing['threshold']) != (values[0], values[2], values[3])
                cur.execute('''UPDATE price_alerts SET ticker=%s,label=%s,condition=%s,threshold=%s,expires_at=%s,
                    triggered=CASE WHEN %s THEN FALSE ELSE triggered END WHERE id=%s''',
                    (*values, rearm, existing['id']))
                if rearm:
                    cur.execute("UPDATE alert_notification_events SET status='cancelled',updated_at=NOW() "
                                "WHERE alert_id=%s AND status IN ('failed','not_configured','pending','unknown')", (existing['id'],))
                alert_id = existing['id']
            else:
                cur.execute('''INSERT INTO price_alerts (ticker,label,condition,threshold,expires_at,source,tier,pinned)
                    VALUES (%s,%s,%s,%s,%s,'manual',2,TRUE) RETURNING id''', values)
                alert_id = cur.fetchone()[0]
            alert = dict(id=alert_id, source='manual', source_key=None) if existing is None else existing
            _audit(cur, alert, 'manual_create' if existing is None else 'manual_edit',
                   {'before': {k: existing.get(k) for k in ('ticker','label','condition','threshold','expires_at')} if existing else None,
                    'after': dict(zip(('ticker','label','condition','threshold','expires_at'), values))})
        conn.commit()
        return alert_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
