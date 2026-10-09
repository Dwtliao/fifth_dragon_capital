"""Opt-in PostgreSQL tests; private fixtures in a disposable schema, mocked SMTP/quotes.

Set ALERT_TEST_DATABASE_URL to an isolated test database. Never enables live delivery.
"""
import os
from pathlib import Path
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg2
from psycopg2 import sql

os.environ.setdefault('DATABASE_URL', 'postgresql://localhost/fake')
from alerts import lifecycle, poller
from alerts.notify import DeliveryResult

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.environ.get('ALERT_TEST_DATABASE_URL'), 'Requires isolated PostgreSQL test database')
class AlertControlsDatabaseTests(unittest.TestCase):
    def connect(self):
        return psycopg2.connect(os.environ['ALERT_TEST_DATABASE_URL'],
                               options=f'-c search_path={self.schema}')

    def setUp(self):
        self.schema = 'alert_test_' + uuid.uuid4().hex
        self.admin = psycopg2.connect(os.environ['ALERT_TEST_DATABASE_URL'])
        self.admin.autocommit = True
        with self.admin.cursor() as cur:
            cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.cleanup_schema)
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                for filename in ('065_price_alerts.sql', '069_price_alerts_source_tier_expiry.sql',
                                 '070_price_alerts_lifecycle.sql', '071_price_alerts_source_key_unique.sql',
                                 '072_price_alerts_recurrence.sql',
                                 '075_alert_controls.sql'):
                    cur.execute((ROOT / 'data_model' / filename).read_text())
            conn.commit()
        finally:
            conn.close()
        self.patchers = [patch('alerts.lifecycle.get_connection', self.connect),
                         patch('alerts.poller.get_connection', self.connect),
                         patch('alerts.poller._fetch_prices', return_value={'TEST': 101.0}),
                         patch('alerts.poller.deliver_alert_email', return_value=DeliveryResult('sent'))]
        self.mocks = [self.enterContext(p) for p in self.patchers]
        self.delivery = self.mocks[-1]

    def cleanup_schema(self):
        # Only drop the freshly created, uniquely named fixture schema we own.
        with self.admin.cursor() as cur:
            cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.admin.close()

    def execute(self, statement, params=(), fetch=False):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(statement, params)
                rows = cur.fetchall() if fetch else None
            conn.commit()
            return rows
        finally:
            conn.close()

    def add_alert(self, source='manual', source_key=None, **values):
        row = self.execute("""INSERT INTO price_alerts (ticker, condition, threshold, source, source_key,
            enabled, archived_at, expires_at, triggered) VALUES ('TEST','above',100,%s,%s,%s,%s,%s,%s)
            RETURNING id""", (source, source_key, values.get('enabled', True), values.get('archived_at'),
                               values.get('expires_at'), values.get('triggered', False)), True)
        return row[0][0]

    def alert(self, alert_id):
        conn = self.connect()
        try:
            return lifecycle.load_alerts(conn, alert_id=alert_id)[0]
        finally:
            conn.close()

    def test_pause_survives_real_compiler_refresh_and_recreation(self):
        from morning_brief.alert_compiler import reconcile_structural_alerts
        desired = {'positions': {}, 'watch': {'TEST': {'resistance': 100}}}
        with patch('morning_brief.alert_compiler.get_connection', self.connect):
            reconcile_structural_alerts(desired)
            alert_id = self.execute('SELECT id FROM price_alerts', fetch=True)[0][0]
            lifecycle.control_alert(alert_id, 'pause')
            desired['watch']['TEST']['resistance'] = 102
            reconcile_structural_alerts(desired)
            reconcile_structural_alerts(desired)
            self.assertEqual(self.alert(alert_id)['threshold'], 102)
            self.assertFalse(lifecycle.is_eligible(self.alert(alert_id)))
            self.execute('DELETE FROM price_alerts WHERE id=%s', (alert_id,))
            reconcile_structural_alerts(desired)
            new_id = self.execute('SELECT id FROM price_alerts', fetch=True)[0][0]
            self.assertNotEqual(new_id, alert_id)
            self.assertFalse(lifecycle.is_eligible(self.alert(new_id)))

    def test_snooze_expiration_and_resume(self):
        alert_id = self.add_alert()
        lifecycle.control_alert(alert_id, 'snooze', snoozed_until=datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertFalse(lifecycle.is_eligible(self.alert(alert_id)))
        self.execute("UPDATE alert_controls SET snoozed_until=NOW()-INTERVAL '1 second'")
        self.assertTrue(lifecycle.is_eligible(self.alert(alert_id)))
        lifecycle.control_alert(alert_id, 'pause')
        lifecycle.control_alert(alert_id, 'resume')
        self.assertTrue(lifecycle.is_eligible(self.alert(alert_id)))
        self.assertEqual(self.execute('SELECT count(*) FROM alert_action_history', fetch=True)[0][0], 3)

    def test_expired_archived_disabled_and_paused_never_send(self):
        self.add_alert(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        self.add_alert(archived_at=datetime.now(timezone.utc))
        self.add_alert(enabled=False)
        alert_id = self.add_alert()
        lifecycle.control_alert(alert_id, 'pause')
        self.assertEqual(poller.run_once(), 0)
        self.delivery.assert_not_called()

    def test_delivery_success_not_repeated_and_rearms(self):
        alert_id = self.add_alert()
        self.assertEqual(poller.run_once(), 1)
        self.assertEqual(poller.run_once(), 0)
        self.assertEqual(self.delivery.call_count, 1)
        self.assertIsNotNone(self.alert(alert_id)['last_fired_at'])
        with patch('alerts.poller._fetch_prices', return_value={'TEST': 99.0}):
            poller.run_once()
        self.assertFalse(self.alert(alert_id)['triggered'])
        self.assertEqual(poller.run_once(), 1)

    def test_failed_delivery_retries_with_bound_and_no_false_success(self):
        alert_id = self.add_alert()
        self.delivery.return_value = DeliveryResult('failed', 'SMTP unavailable')
        self.assertEqual(poller.run_once(), 0)
        self.assertIsNone(self.alert(alert_id)['last_fired_at'])
        self.assertEqual(self.alert(alert_id)['delivery_status'], 'failed')
        poller.run_once()
        self.assertEqual(self.delivery.call_count, 1)
        for _ in range(4):
            self.execute("UPDATE alert_notification_events SET next_attempt_at=NOW()-INTERVAL '1 second'")
            poller.run_once()
        self.assertEqual(self.delivery.call_count, 3)
        self.assertEqual(self.alert(alert_id)['delivery_attempts'], 3)

    def test_unconfigured_delivery_recovers_when_configured(self):
        alert_id = self.add_alert()
        self.delivery.return_value = DeliveryResult('not_configured', 'Incomplete configuration')
        poller.run_once()
        with patch('alerts.poller.email_configured', return_value=False):
            poller.run_once()
        self.assertEqual(self.delivery.call_count, 1)
        self.assertIsNone(self.alert(alert_id)['last_fired_at'])
        self.delivery.return_value = DeliveryResult('sent')
        with patch('alerts.poller.email_configured', return_value=True):
            self.assertEqual(poller.run_once(), 1)

    def test_interrupted_delivery_requires_review_not_automatic_resend(self):
        alert_id = self.add_alert(triggered=True)
        self.execute("""INSERT INTO alert_notification_events
            (alert_id,ticker,condition,threshold,price,status,attempts)
            VALUES (%s,'TEST','above',100,101,'pending',1)""", (alert_id,))
        poller.run_once()
        poller.run_once()
        self.assertEqual(self.alert(alert_id)['delivery_status'], 'unknown')
        self.delivery.assert_not_called()

    def test_parallel_poll_is_skipped(self):
        self.add_alert()
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_lock(%s,%s)', (lifecycle.LOCK_NAMESPACE, lifecycle.POLL_LOCK))
            conn.commit()
            self.assertEqual(poller.run_once(), 0)
            self.delivery.assert_not_called()
            self.assertEqual(self.execute('SELECT count(*) FROM alert_poll_runs', fetch=True)[0][0], 0)
        finally:
            conn.close()

    def test_dry_run_and_missing_quotes_do_not_send_or_trigger(self):
        alert_id = self.add_alert()
        poller.run_once(dry_run=True)
        self.assertFalse(self.alert(alert_id)['triggered'])
        self.assertEqual(self.execute('SELECT count(*) FROM alert_poll_runs', fetch=True)[0][0], 0)
        with patch('alerts.poller._fetch_prices', return_value={'TEST': float('nan')}):
            poller.run_once()
        self.assertFalse(self.alert(alert_id)['triggered'])
        self.delivery.assert_not_called()

    def test_migration_preserves_rows_and_legacy_suppression_and_is_repeatable(self):
        alert_id = self.add_alert(source='key_levels_watch', source_key='watch:TEST:resistance', enabled=False)
        before = self.execute('SELECT * FROM price_alerts', fetch=True)
        self.execute((ROOT / 'data_model/075_alert_controls.sql').read_text())
        self.execute((ROOT / 'data_model/075_alert_controls.sql').read_text())
        self.assertEqual(before, self.execute('SELECT * FROM price_alerts', fetch=True))
        self.execute('UPDATE price_alerts SET enabled=TRUE WHERE id=%s', (alert_id,))
        self.assertFalse(lifecycle.is_eligible(self.alert(alert_id)))

    def test_managed_edits_and_expired_resume_are_rejected(self):
        managed = self.add_alert(source='key_levels_watch', source_key='watch:TEST:resistance')
        for action in ('threshold', 'archive'):
            with self.assertRaises(ValueError):
                lifecycle.control_alert(managed, action, threshold=123)
        expired = self.add_alert(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            lifecycle.control_alert(expired, 'resume')

    def test_pause_during_quote_fetch_is_rechecked(self):
        alert_id = self.add_alert()
        def fetch(tickers):
            lifecycle.control_alert(alert_id, 'pause')
            return {'TEST': 101.0}
        with patch('alerts.poller._fetch_prices', side_effect=fetch):
            poller.run_once()
        self.delivery.assert_not_called()

    def test_smtp_runs_without_open_database_transaction(self):
        from psycopg2.extensions import TRANSACTION_STATUS_IDLE
        alert_id = self.add_alert()
        conn = self.connect()
        def deliver(*args):
            self.assertEqual(conn.get_transaction_status(), TRANSACTION_STATUS_IDLE)
            return DeliveryResult('sent')
        try:
            with patch('alerts.poller.deliver_alert_email', side_effect=deliver):
                self.assertEqual(poller._process_alert(conn, alert_id, {'TEST': 101.0}), 'sent')
        finally:
            conn.close()

    def test_manual_archive_restore_and_cancel_failed_retry(self):
        alert_id = self.add_alert()
        self.delivery.return_value = DeliveryResult('failed', 'SMTP unavailable')
        poller.run_once()
        lifecycle.control_alert(alert_id, 'archive')
        self.assertEqual(lifecycle.state(self.alert(alert_id)), 'Archived')
        self.assertEqual(self.alert(alert_id)['delivery_status'], 'cancelled')
        lifecycle.control_alert(alert_id, 'resume')
        self.assertTrue(lifecycle.is_eligible(self.alert(alert_id)))
        self.delivery.return_value = DeliveryResult('sent')
        self.assertEqual(poller.run_once(), 1)

    def test_poll_failure_is_persisted(self):
        self.add_alert()
        with patch('alerts.poller._fetch_prices', side_effect=RuntimeError('provider unavailable')):
            with self.assertRaises(RuntimeError):
                poller.run_once()
        rows = self.execute('SELECT status, finished_at, error FROM alert_poll_runs', fetch=True)
        self.assertEqual(rows[0][0], 'failed')
        self.assertIsNotNone(rows[0][1])
        self.assertIn('RuntimeError', rows[0][2])

    def test_journal_reconciliation_and_promotion_preserve_pause(self):
        from morning_brief.alert_compiler import reconcile_journal_alerts
        desired = [{'ticker': 'TEST', 'condition': 'above', 'threshold': 100, 'label': 'Journal level'}]
        with patch('morning_brief.alert_compiler.get_connection', self.connect):
            reconcile_journal_alerts(desired)
            alert_id = self.execute('SELECT id FROM price_alerts', fetch=True)[0][0]
            lifecycle.control_alert(alert_id, 'pause')
            reconcile_journal_alerts(desired)
            reconcile_journal_alerts(desired)
            alert = self.alert(alert_id)
            self.assertEqual(alert['source'], 'key_levels_watch')
            self.assertTrue(alert['paused'])
            self.assertFalse(lifecycle.is_eligible(alert))

    def test_browser_controls_render_pause_and_resume(self):
        import pandas as pd
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        from unittest.mock import MagicMock
        alert_id = self.add_alert()
        ticker = MagicMock()
        ticker.fast_info.last_price = 101.0
        ticker.history.return_value = pd.DataFrame()
        st.cache_data.clear()
        with patch('dashboard.db.get_connection', self.connect), \
             patch('morning_brief.alert_compiler.find_duplicate_alerts', return_value=[]), \
             patch('morning_brief.alert_compiler.find_stale_alerts', return_value=[]), \
             patch('yfinance.Ticker', return_value=ticker):
            app = AppTest.from_file(str(ROOT / 'dashboard/pages/P7_Market_Monitor.py'), default_timeout=20).run()
            self.assertEqual(len(app.exception), 0)
            app.button(key='active_pause').click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(lifecycle.state(self.alert(alert_id)), 'Paused')
            app.checkbox(key=f'resume_confirm_{alert_id}').check().run()
            app.button(key='inactive_resume').click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(lifecycle.is_eligible(self.alert(alert_id)))


if __name__ == '__main__':
    unittest.main()
