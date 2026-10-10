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
                                 '075_alert_controls.sql', '076_alert_purge.sql'):
                    cur.execute((ROOT / 'data_model' / filename).read_text())
            conn.commit()
        finally:
            conn.close()
        self.patchers = [patch('alerts.lifecycle.get_connection', self.connect),
                         patch('alerts.management.get_connection', self.connect),
                         patch('alerts.poller.get_connection', self.connect),
                         patch('alerts.poller._fetch_prices', return_value={'TEST': 101.0}),
                         patch('morning_brief.alert_compiler._held_symbols', return_value=set()),
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
            enabled, archived_at, expires_at, triggered) VALUES ('TEST','above',%s,%s,%s,%s,%s,%s,%s)
            RETURNING id""", (values.get('threshold', 100), source, source_key, values.get('enabled', True), values.get('archived_at'),
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
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        from types import SimpleNamespace
        alert_id = self.add_alert()
        st.cache_data.clear()
        original_dataframe = st.dataframe
        selected_indices = []
        def dataframe(*args, **kwargs):
            displayed = original_dataframe(*args, **kwargs)
            if kwargs.get('on_select') == 'rerun':
                return SimpleNamespace(selection=SimpleNamespace(rows=selected_indices))
            return displayed
        with patch('dashboard.db.get_connection', self.connect), \
             patch('streamlit.dataframe', side_effect=dataframe), \
             patch('dashboard.alerts_workspace.quote_snapshot') as quotes, \
             patch('morning_brief.alert_compiler.find_duplicate_alerts') as duplicates:
            app = AppTest.from_file(str(ROOT / 'dashboard/pages/P11_Alerts.py'), default_timeout=20).run()
            self.assertEqual(len(app.exception), 0)
            quotes.assert_not_called()
            duplicates.assert_not_called()
            def select_first():
                # AppTest cannot emit native grid-selection events. Simulate only
                # that event; render the real grid and exercise all actual buttons.
                selected_indices[:] = [0]
                app.run()
            select_first()
            app.button(key='workspace_preview_button').click().run()
            confirm = next(c for c in app.checkbox if c.label.startswith('Confirm pause'))
            confirm.check().run()
            app.button(key='workspace_apply').click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(lifecycle.state(self.alert(alert_id)), 'Paused')
            select_first()
            app.selectbox(key='workspace_action').select('resume').run()
            app.button(key='workspace_preview_button').click().run()
            confirm = next(c for c in app.checkbox if c.label.startswith('Confirm resume'))
            confirm.check().run()
            app.button(key='workspace_apply').click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(lifecycle.is_eligible(self.alert(alert_id)))
            quotes.assert_not_called()
            app.selectbox(key='workspace_action').select('pause').run()
            app.button(key='workspace_preview_button').click().run()
            before = self.execute('SELECT count(*) FROM alert_action_history', fetch=True)
            app.button(key='workspace_cancel').click().run()
            self.assertEqual(before, self.execute('SELECT count(*) FROM alert_action_history', fetch=True))
            app.text_input(key='workspace_search').input('no matching symbol').run()
            self.assertEqual(len(app.exception), 0)
            self.assertNotIn('workspace_preview', app.session_state)
            quotes.assert_not_called()
            app.text_input(key='workspace_search').input('').run()
            select_first()
            app.selectbox(key='workspace_action').select('purge').run()
            app.button(key='workspace_preview_button').click().run()
            next(c for c in app.checkbox if c.label.startswith('Confirm purge')).check().run()
            app.button(key='workspace_apply').click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(self.execute('SELECT count(*) FROM price_alerts WHERE id=%s', (alert_id,), True)[0][0], 0)

    def test_bulk_preview_rejects_changed_target_without_partial_writes(self):
        from alerts.management import apply_bulk, preview_action
        first, second = self.add_alert(), self.add_alert()
        preview = preview_action([self.alert(first), self.alert(second)], 'pause')
        self.execute('UPDATE price_alerts SET threshold=102 WHERE id=%s', (second,))
        with self.assertRaises(ValueError):
            apply_bulk(preview, 'pause')
        self.assertFalse(self.alert(first)['paused'])
        self.assertEqual(self.execute('SELECT count(*) FROM alert_action_history', fetch=True)[0][0], 0)

    def test_bulk_exact_ids_and_managed_archive_guard(self):
        from alerts.management import apply_bulk, preview_action
        first, second, hidden = self.add_alert(), self.add_alert(), self.add_alert()
        preview = preview_action([self.alert(first), self.alert(second)], 'pause')
        self.assertEqual(apply_bulk(preview, 'pause'), [first, second])
        self.assertFalse(self.alert(hidden)['paused'])
        managed = self.add_alert(source='key_levels_watch', source_key='watch:TEST:level')
        preview = preview_action([self.alert(first), self.alert(managed)], 'archive')
        with self.assertRaises(ValueError):
            apply_bulk(preview, 'archive')
        self.assertIsNone(self.alert(first)['archived_at'])

    def test_bulk_snooze_and_resume_preserve_managed_level(self):
        from alerts.management import apply_bulk, preview_action
        alert_id = self.add_alert(source='key_levels_watch', source_key='watch:TEST:resistance')
        until = datetime.now(timezone.utc) + timedelta(hours=1)
        apply_bulk(preview_action([self.alert(alert_id)], 'snooze'), 'snooze', snoozed_until=until)
        self.assertEqual(self.alert(alert_id)['snoozed_until'], until)
        self.assertFalse(lifecycle.is_eligible(self.alert(alert_id)))
        apply_bulk(preview_action([self.alert(alert_id)], 'resume'), 'resume')
        self.assertTrue(lifecycle.is_eligible(self.alert(alert_id)))
        self.assertEqual(self.alert(alert_id)['threshold'], 100)

    def test_purge_all_sources_and_states_preserves_audit(self):
        from alerts.management import apply_bulk, preview_action
        manual = self.add_alert(archived_at=datetime.now(timezone.utc))
        journal = self.add_alert(source='journal_sync', source_key='journal:TEST:old',
                                 expires_at=datetime.now(timezone.utc)-timedelta(days=1))
        structural = self.add_alert(source='key_levels_watch', source_key='watch:TEST:resistance')
        hidden = self.add_alert()
        preview = preview_action([self.alert(i) for i in (manual, journal, structural)], 'purge')
        apply_bulk(preview, 'purge')
        self.assertEqual(self.execute('SELECT id FROM price_alerts ORDER BY id', fetch=True), [(hidden,)])
        self.assertEqual(self.execute("SELECT count(*) FROM alert_action_history WHERE action='purge'", fetch=True)[0][0], 3)

    def test_held_policy_archives_watch_and_journal_preserves_stop_and_manual(self):
        from morning_brief.alert_compiler import reconcile_structural_alerts, reconcile_journal_alerts
        watch = self.add_alert(source='key_levels_watch', source_key='watch:TEST:resistance')
        stop = self.add_alert(source='key_levels_watch', source_key='position:TEST:stop', threshold=90)
        journal = self.add_alert(source='journal_sync', source_key='journal:TEST:idea')
        manual = self.add_alert()
        levels = {'positions': {'TEST': {'stop': 90}}, 'watch': {'TEST': {'resistance': 100}}}
        with patch('morning_brief.alert_compiler.get_connection', self.connect), \
             patch('morning_brief.alert_compiler._held_symbols', return_value={'TEST'}):
            stats = reconcile_structural_alerts(levels)
            journal_stats = reconcile_journal_alerts([{'ticker': 'TEST', 'condition': 'above', 'threshold': 105}])
            self.assertEqual(journal_stats['skipped_held'], 1)
            self.assertGreaterEqual(stats['archived'] + stats['held_archived'], 2)
            self.assertIsNotNone(self.alert(watch)['archived_at'])
            self.assertIsNotNone(self.alert(journal)['archived_at'])
            self.assertTrue(lifecycle.is_eligible(self.alert(stop)))
            self.assertTrue(lifecycle.is_eligible(self.alert(manual)))
            self.assertEqual(self.execute("SELECT count(*) FROM price_alerts", fetch=True)[0][0], 4)
        with patch('morning_brief.alert_compiler.get_connection', self.connect):
            reconcile_structural_alerts(levels)
        self.assertTrue(lifecycle.is_eligible(self.alert(watch)))

    def test_held_policy_dry_run_does_not_archive(self):
        from morning_brief.alert_compiler import reconcile_journal_alerts
        journal = self.add_alert(source='journal_sync', source_key='journal:TEST:idea')
        with patch('morning_brief.alert_compiler.get_connection', self.connect), \
             patch('morning_brief.alert_compiler._held_symbols', return_value={'TEST'}):
            stats = reconcile_journal_alerts([], dry_run=True)
        self.assertEqual(stats['held_archived'], 1)
        self.assertTrue(lifecycle.is_eligible(self.alert(journal)))
        self.delivery.assert_not_called()

    def test_purged_managed_and_journal_levels_do_not_reappear(self):
        from alerts.management import apply_bulk, preview_action
        from morning_brief.alert_compiler import reconcile_structural_alerts, reconcile_journal_alerts
        structural = self.add_alert(source='key_levels_watch', source_key='watch:TEST:resistance')
        journal = self.add_alert(source='journal_sync', source_key='journal:OTHER:old')
        self.execute("UPDATE price_alerts SET ticker='OTHER' WHERE id=%s", (journal,))
        apply_bulk(preview_action([self.alert(structural), self.alert(journal)], 'purge'), 'purge')
        with patch('morning_brief.alert_compiler.get_connection', self.connect):
            stats = reconcile_structural_alerts({'positions': {}, 'watch': {'TEST': {'resistance': 105}}})
            self.assertEqual(stats['purged'], 1)
            stats = reconcile_journal_alerts([{'ticker':'OTHER','condition':'above','threshold':100.2}])
            self.assertEqual(stats['purged'], 1)
        self.assertEqual(self.execute('SELECT count(*) FROM price_alerts', fetch=True)[0][0], 0)
        # A genuinely different journal idea or an explicitly added manual level is allowed.
        with patch('morning_brief.alert_compiler.get_connection', self.connect):
            stats = reconcile_journal_alerts([{'ticker':'OTHER','condition':'above','threshold':110}])
            self.assertEqual(stats['created'], 1)
        self.add_alert()

    def test_purge_rejects_changed_target_without_deleting_anything(self):
        from alerts.management import apply_bulk, preview_action
        first, second = self.add_alert(), self.add_alert()
        preview = preview_action([self.alert(first), self.alert(second)], 'purge')
        self.execute('UPDATE price_alerts SET threshold=102 WHERE id=%s', (second,))
        with self.assertRaises(ValueError):
            apply_bulk(preview, 'purge')
        self.assertEqual(self.execute('SELECT count(*) FROM price_alerts', fetch=True)[0][0], 2)

    def test_purge_retains_notification_snapshot_and_handles_invalid_level(self):
        from alerts.management import apply_bulk, preview_action
        first = self.add_alert()
        self.execute("""INSERT INTO alert_notification_events
            (alert_id,ticker,condition,threshold,price,status) VALUES (%s,'TEST','above',100,101,'sent')""", (first,))
        invalid = self.add_alert(threshold=float('nan'))
        apply_bulk(preview_action([self.alert(first), self.alert(invalid)], 'purge'), 'purge')
        self.assertEqual(self.execute('SELECT count(*) FROM alert_notification_events', fetch=True)[0][0], 0)
        details = self.execute("SELECT details FROM alert_action_history WHERE alert_id=%s AND action='purge'", (first,), True)[0][0]
        self.assertEqual(details['notifications'][0]['status'], 'sent')

    def test_purged_structural_identity_cannot_reappear_via_promotion(self):
        from alerts.management import apply_bulk, preview_action
        from morning_brief.alert_compiler import reconcile_journal_alerts
        structural = self.add_alert(source='key_levels_watch', source_key='watch:TEST:resistance')
        apply_bulk(preview_action([self.alert(structural)], 'purge'), 'purge')
        with patch('morning_brief.alert_compiler.get_connection', self.connect):
            for _ in range(3):
                reconcile_journal_alerts([{'ticker':'TEST','condition':'above','threshold':105}])
        self.assertEqual(self.execute("SELECT count(*) FROM price_alerts WHERE source='key_levels_watch'", fetch=True)[0][0], 0)

    def test_exact_consolidation_preserves_structural_and_suppresses_duplicate(self):
        from alerts.management import apply_bulk, consolidation_preview
        journal = self.add_alert(source='journal_sync', source_key='journal:TEST:idea')
        structural = self.add_alert(source='key_levels_watch', source_key='watch:TEST:resistance')
        before = self.alert(structural)
        preview = consolidation_preview([self.alert(journal), before])
        apply_bulk(preview, 'consolidate')
        self.assertEqual(before, self.alert(structural))
        self.assertTrue(self.alert(journal)['paused'])
        self.assertIsNotNone(self.alert(journal)['archived_at'])
        self.execute('UPDATE price_alerts SET enabled=TRUE,archived_at=NULL WHERE id=%s', (journal,))
        self.assertFalse(lifecycle.is_eligible(self.alert(journal)))

    def test_consolidation_rejects_changed_pair(self):
        from alerts.management import apply_bulk, consolidation_preview
        manual = self.add_alert()
        structural = self.add_alert(source='key_levels_watch', source_key='watch:TEST:resistance')
        preview = consolidation_preview([self.alert(manual), self.alert(structural)])
        self.execute('UPDATE price_alerts SET threshold=100.01 WHERE id=%s', (structural,))
        with self.assertRaises(ValueError):
            apply_bulk(preview, 'consolidate')
        self.assertIsNone(self.alert(manual)['archived_at'])

    def test_manual_create_edit_duplicates_and_preserved_pause(self):
        from alerts.management import save_manual, fingerprint
        alert_id = save_manual('TEST', 'Manual level', 'above', 100)
        with self.assertRaises(ValueError):
            save_manual('TEST', 'Duplicate', 'above', 100)
        duplicate = save_manual('TEST', 'Intentional duplicate', 'above', 100, allow_duplicate=True)
        self.assertNotEqual(alert_id, duplicate)
        lifecycle.control_alert(alert_id, 'pause')
        snapshot = self.alert(alert_id)
        save_manual('TEST', 'Updated', 'above', 102, expected=dict(id=alert_id, fingerprint=fingerprint(snapshot)))
        updated = self.alert(alert_id)
        self.assertEqual(updated['threshold'], 102)
        self.assertTrue(updated['paused'])
        with self.assertRaises(ValueError):
            save_manual('TEST', 'Stale edit', 'above', 103, expected=dict(id=alert_id, fingerprint=fingerprint(snapshot)))

    def test_p7_renders_charts_without_quote_ranking_or_management(self):
        import pandas as pd
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        from unittest.mock import MagicMock
        self.add_alert()
        ticker = MagicMock()
        ticker.history.return_value = pd.DataFrame()
        st.cache_data.clear()
        with patch('dashboard.db.get_connection', self.connect), \
             patch('yfinance.Ticker', return_value=ticker), \
             patch('streamlit.page_link'), \
             patch('streamlit.delta_generator.DeltaGenerator.page_link'), \
             patch('dashboard.alerts_workspace.quote_snapshot') as quotes:
            app = AppTest.from_file(str(ROOT / 'dashboard/pages/P7_Market_Monitor.py'), default_timeout=20).run()
            self.assertEqual(len(app.exception), 0)
            self.assertNotIn('manage_active_select', [s.key for s in app.selectbox])
            quotes.assert_not_called()

    def test_exception_review_is_explicit_read_only_and_survives_filters(self):
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        self.add_alert()
        before = self.execute('SELECT * FROM price_alerts ORDER BY id', fetch=True)
        st.cache_data.clear()
        with patch('dashboard.db.get_connection', self.connect), \
             patch('dashboard.alerts_workspace.quote_snapshot', return_value={
                 'TEST': dict(price=10, retrieved_at=datetime.now(timezone.utc))}) as quotes, \
             patch('dashboard.alerts_workspace.split_snapshot', return_value={
                 'TEST': dict(events=[], error=None)}) as splits:
            app = AppTest.from_file(str(ROOT / 'dashboard/pages/P11_Alerts.py'), default_timeout=20).run()
            self.assertEqual(len(app.exception), 0)
            quotes.assert_not_called()
            splits.assert_not_called()
            app.button(key='workspace_exception_review').click().run()
            self.assertEqual(len(app.exception), 0)
            report = app.session_state['workspace_exception_report']
            self.assertEqual(report['rows'][0]['Distance %'], 900)
            quotes.assert_called_once_with(('TEST',))
            splits.assert_called_once_with(('TEST',))
            app.text_input(key='workspace_search').input('no matches').run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.session_state['workspace_exception_report'], report)
            self.assertEqual(quotes.call_count, 1)
        self.assertEqual(before, self.execute('SELECT * FROM price_alerts ORDER BY id', fetch=True))
        self.assertEqual(self.execute('SELECT count(*) FROM alert_action_history', fetch=True)[0][0], 0)
        self.delivery.assert_not_called()


if __name__ == '__main__':
    unittest.main()
