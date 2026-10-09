"""Price alerts: python -m alerts.poller --once [--dry-run]."""
import argparse
import json
import time

import yfinance as yf

from alerts.lifecycle import (LOCK_NAMESPACE, POLL_LOCK, alert_lock, is_eligible,
                              load_alerts, valid_price)
from alerts.notify import DeliveryResult, deliver_alert_email, email_configured
from etrade_sync.db import get_connection

DEFAULT_INTERVAL = 300
MAX_ATTEMPTS = 3


def _load_alerts(conn):
    return load_alerts(conn, eligible_only=True)


def _fetch_prices(tickers):
    prices = {}
    for ticker in tickers:
        try:
            price = yf.Ticker(ticker).fast_info.last_price
            if valid_price(price):
                prices[ticker] = float(price)
        except Exception:
            pass
    return prices


def condition_met(alert, price):
    if not valid_price(price) or not valid_price(alert['threshold']):
        return False
    if alert['condition'] == 'above':
        return price > alert['threshold']
    if alert['condition'] == 'below':
        return price < alert['threshold']
    return False


def _latest_event(conn, alert_id):
    with conn.cursor() as cur:
        cur.execute('SELECT *, next_attempt_at IS NULL OR next_attempt_at <= NOW() AS due '
                    'FROM alert_notification_events WHERE alert_id=%s ORDER BY id DESC LIMIT 1', (alert_id,))
        cols = [d.name for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None


def _process_alert(conn, alert_id, prices):
    """Each claim and outcome is committed before/after SMTP."""
    with alert_lock(conn, alert_id):
        rows = load_alerts(conn, alert_id=alert_id)
        if not rows or not is_eligible(rows[0]):
            return 'suppressed'
        alert = rows[0]
        price = prices.get(alert['ticker'])
        if not valid_price(price) or not valid_price(alert['threshold']):
            return 'missing_quote'
        event = _latest_event(conn, alert_id)
        with conn.cursor() as cur:
            if not condition_met(alert, price):
                cur.execute('UPDATE price_alerts SET triggered=FALSE WHERE id=%s', (alert_id,))
                cur.execute("UPDATE alert_notification_events SET status='cancelled', updated_at=NOW() "
                            "WHERE alert_id=%s AND status IN ('failed','not_configured','pending','unknown')", (alert_id,))
                conn.commit()
                return 'armed'
            changed = event and (event['ticker'] != alert['ticker'] or
                event['condition'] != alert['condition'] or float(event['threshold']) != alert['threshold'])
            if changed:
                cur.execute("UPDATE alert_notification_events SET status='cancelled', updated_at=NOW() "
                            "WHERE id=%s AND status <> 'sent'", (event['id'],))
                event = None
            if alert['triggered'] and event and not changed and event['status'] == 'pending':
                cur.execute("UPDATE alert_notification_events SET status='unknown', "
                            "error='Previous worker stopped during delivery; review before rearming', "
                            "updated_at=NOW() WHERE id=%s", (event['id'],))
                conn.commit()
                return 'unknown'
            if alert['triggered'] and not changed:
                if event is None or event['status'] in ('sent', 'unknown'):
                    conn.commit()
                    return 'condition_met'
                if event['status'] == 'failed' and (event['attempts'] >= MAX_ATTEMPTS or not event['due']):
                    conn.commit()
                    return 'retry_wait'
                if event['status'] == 'not_configured' and not email_configured():
                    conn.commit()
                    return 'not_configured'
            if not alert['triggered'] or event is None or event['status'] == 'cancelled':
                cur.execute("""INSERT INTO alert_notification_events
                    (alert_id,ticker,label,condition,threshold,price,status)
                    VALUES (%s,%s,%s,%s,%s,%s,'pending') RETURNING id""",
                    (alert_id, alert['ticker'], alert['label'], alert['condition'], alert['threshold'], price))
                event = {'id': cur.fetchone()[0], 'attempts': 0}
            attempts = event['attempts'] + 1
            cur.execute("UPDATE alert_notification_events SET status='pending', attempts=%s, price=%s, "
                        "updated_at=NOW() WHERE id=%s", (attempts, price, event['id']))
            cur.execute('UPDATE price_alerts SET triggered=TRUE WHERE id=%s', (alert_id,))
        conn.commit()
        try:
            result = deliver_alert_email(alert['ticker'], alert['label'] or '',
                                         alert['condition'], alert['threshold'], price)
        except Exception as exc:
            result = DeliveryResult('failed', f'Delivery failed ({type(exc).__name__})')
        with conn.cursor() as cur:
            cur.execute("""UPDATE alert_notification_events SET status=%s, error=%s,
                next_attempt_at=CASE WHEN %s='failed' AND attempts < %s
                    THEN NOW() + (%s * INTERVAL '5 minutes') ELSE NULL END,
                updated_at=NOW() WHERE id=%s""",
                (result.status, result.error, result.status, MAX_ATTEMPTS, attempts, event['id']))
            if result.status == 'sent':
                cur.execute('UPDATE price_alerts SET last_fired_at=NOW() WHERE id=%s', (alert_id,))
        conn.commit()
        print(f"  {alert['ticker']}: condition met — delivery {result.status}")
        return result.status


def run_once(dry_run=False):
    """Return emails delivered. Dry-run never sends or writes."""
    conn = get_connection()
    run_id, locked = None, False
    summary = {'eligible': 0, 'missing_quotes': [], 'outcomes': {}}
    try:
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_try_advisory_lock(%s,%s)', (LOCK_NAMESPACE, POLL_LOCK))
                locked = cur.fetchone()[0]
                if not locked:
                    print('Another poll is running — skipped (no notifications sent).')
                    return 0
                cur.execute("UPDATE alert_poll_runs SET status='interrupted', finished_at=NOW(), "
                            "error='Worker stopped before completion' WHERE status='running'")
                cur.execute('INSERT INTO alert_poll_runs DEFAULT VALUES RETURNING id')
                run_id = cur.fetchone()[0]
            conn.commit()
        alerts = _load_alerts(conn)
        conn.commit()
        tickers = sorted({a['ticker'] for a in alerts})
        summary['eligible'] = len(alerts)
        print(f"{'DRY RUN — ' if dry_run else ''}Polling {len(alerts)} eligible alert(s), "
              f'{len(tickers)} ticker(s); Yahoo quotes may be delayed.')
        prices = _fetch_prices(tickers)
        summary['missing_quotes'] = [t for t in tickers if not valid_price(prices.get(t))]
        for ticker in summary['missing_quotes']:
            print(f'  {ticker}: no valid quote — skipped')
        for alert in alerts:
            if dry_run:
                price = prices.get(alert['ticker'])
                print(f"  #{alert['id']} {alert['ticker']}: " +
                      ('no quote' if not valid_price(price) else
                       'condition met' if condition_met(alert, price) else 'armed'))
                continue
            outcome = _process_alert(conn, alert['id'], prices)
            summary['outcomes'][outcome] = summary['outcomes'].get(outcome, 0) + 1
        if run_id:
            with conn.cursor() as cur:
                cur.execute("UPDATE alert_poll_runs SET finished_at=NOW(), status='complete', summary=%s::jsonb WHERE id=%s",
                            (json.dumps(summary), run_id))
            conn.commit()
        sent = summary['outcomes'].get('sent', 0)
        print(f'Poll complete — {sent} email(s) delivered. Outcomes: {summary["outcomes"]}')
        return sent
    except Exception as exc:
        conn.rollback()
        if run_id:
            with conn.cursor() as cur:
                cur.execute("UPDATE alert_poll_runs SET finished_at=NOW(), status='failed', error=%s, summary=%s::jsonb WHERE id=%s",
                            (f'Poll failed ({type(exc).__name__})', json.dumps(summary), run_id))
            conn.commit()
        raise
    finally:
        if locked:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_unlock(%s,%s)', (LOCK_NAMESPACE, POLL_LOCK))
            conn.commit()
        conn.close()


def run_loop(interval):
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f'Poll error: {exc}')
        time.sleep(interval)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Price alert poller')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Single read-only cycle; no email or state changes')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    args = parser.parse_args()
    if args.once or args.dry_run:
        run_once(dry_run=args.dry_run)
    elif args.interval <= 0:
        parser.error('--interval must be positive')
    else:
        run_loop(args.interval)
