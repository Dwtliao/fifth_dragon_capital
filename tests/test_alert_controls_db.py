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

    def test_stop_pause_survives_real_compiler_refresh_and_recreation(self):
        from morning_brief.alert_compiler import reconcile_structural_alerts
        desired = {'positions': {'TEST': {'stop': 100}}, 'watch': {}}
        with patch('morning_brief.alert_compiler.get_connection', self.connect):
            reconcile_structural_alerts(desired)
            alert_id = self.execute('SELECT id FROM price_alerts', fetch=True)[0][0]
            lifecycle.control_alert(alert_id, 'pause')
            desired['positions']['TEST']['stop'] = 102
            reconcile_structural_alerts(desired)
            self.assertEqual(self.alert(alert_id)['threshold'], 102)
            self.assertFalse(lifecycle.is_eligible(self.alert(alert_id)))
            self.execute('DELETE FROM price_alerts WHERE id=%s', (alert_id,))
            reconcile_structural_alerts(desired)
            new_id = self.execute('SELECT id FROM price_alerts', fetch=True)[0][0]
            self.assertNotEqual(new_id, alert_id)
            self.assertFalse(lifecycle.is_eligible(self.alert(new_id)))
            reconcile_structural_alerts({'positions': {}, 'watch': {}})
            self.assertIsNotNone(self.alert(new_id)['archived_at'])

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

    def test_existing_discretionary_rows_are_unchanged_across_all_source_refreshes(self):
        from morning_brief import alert_compiler as compiler
        watch = self.add_alert(source='key_levels_watch', source_key='watch:TEST:resistance', threshold=91)
        journal = self.add_alert(source='journal_sync', source_key='journal:TEST:old', threshold=92,
                                 expires_at=datetime.now(timezone.utc)+timedelta(days=3))
        legacy = self.add_alert()
        self.execute("UPDATE price_alerts SET label='Watch: TEST above 100' WHERE id=%s", (legacy,))
        lifecycle.control_alert(watch, 'pause')
        lifecycle.control_alert(journal, 'snooze', snoozed_until=datetime.now(timezone.utc)+timedelta(hours=2))
        archived = self.add_alert(source='key_levels_watch', source_key='watch:TEST:support',
                                  archived_at=datetime.now(timezone.utc), enabled=False)
        before = {i:self.alert(i) for i in (watch,journal,legacy,archived)}
        with patch.object(compiler, 'get_connection', self.connect):
            for held in (set(), {'TEST'}, set()):
                with patch.object(compiler, '_held_symbols', return_value=held):
                    compiler.reconcile_structural_alerts({'watch':{'TEST':{'resistance':120,'support':80}}})
                    compiler.reconcile_structural_alerts({})
                    compiler.reconcile_journal_alerts([dict(ticker='TEST',condition='above',threshold=125)])
                    compiler.promote_recurring_journal_alerts(threshold=1)
                    compiler.backfill_legacy_structural_alerts()
            # Direct legacy callers cannot bypass the same stop-only boundary, even on reload.
            desired = [dict(source='key_levels_watch',source_key='watch:TEST:resistance',ticker='TEST',
                            condition='above',threshold=130,label='new'),
                       dict(source='journal_sync',source_key='journal:NEW:old',ticker='NEW',
                            condition='above',threshold=15,label='new')]
            compiler.reconcile_alerts(desired, reload_desired=lambda:desired)
        self.assertEqual(before, {i:self.alert(i) for i in before})
        self.assertEqual(self.execute('SELECT count(*) FROM price_alerts',fetch=True)[0][0],4)
        self.delivery.assert_not_called()

    def test_browser_controls_render_pause_and_resume(self):
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        from types import SimpleNamespace
        alert_id = self.add_alert()
        st.cache_data.clear()
        original_editor = st.data_editor
        selected_indices = []
        def editor(*args, **kwargs):
            displayed = original_editor(*args, **kwargs)
            displayed['Select'] = [i in selected_indices for i in range(len(displayed))]
            return displayed
        with patch('dashboard.db.get_connection', self.connect), \
             patch('streamlit.data_editor', side_effect=editor), \
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
            next(c for c in app.checkbox if c.label.startswith('Confirm delete')).check().run()
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

    def test_saved_watch_levels_never_create_alerts_while_stops_still_refresh(self):
        from morning_brief import alert_compiler as compiler
        levels = {'positions': {'TEST': {'stop': 90}},
                  'watch': {'TEST': {'support': 80,'resistance':100,'alert_above':110},
                            'OTHER': {'resistance': 55}}}
        with patch.object(compiler,'get_connection',self.connect):
            for held in (set(), {'TEST'}):
                with patch.object(compiler,'_held_symbols',return_value=held):
                    compiler.reconcile_structural_alerts(levels)
        rows=self.execute('SELECT source_key,threshold FROM price_alerts',fetch=True)
        self.assertEqual(rows,[('position:TEST:stop',90)])

    def test_stop_refresh_dry_run_writes_nothing(self):
        from morning_brief.alert_compiler import reconcile_structural_alerts
        stop=self.add_alert(source='key_levels_watch',source_key='position:TEST:stop',threshold=90)
        before=self.alert(stop)
        with patch('morning_brief.alert_compiler.get_connection',self.connect):
            reconcile_structural_alerts({'positions':{'TEST':{'stop':95}},'watch':{'OTHER':{'support':55}}},dry_run=True)
        self.assertEqual(before,self.alert(stop))

    def test_delete_adds_no_discretionary_blocks_and_refresh_cannot_recreate(self):
        from alerts.management import apply_bulk, preview_action
        from morning_brief import alert_compiler as compiler
        ids=[self.add_alert(source='key_levels_watch',source_key='watch:TEST:resistance'),
             self.add_alert(source='journal_sync',source_key='journal:TEST:old'),self.add_alert()]
        apply_bulk(preview_action([self.alert(i) for i in ids],'purge'),'purge')
        self.assertEqual(self.execute('SELECT count(*) FROM alert_purge_blocks',fetch=True)[0][0],0)
        with patch.object(compiler,'get_connection',self.connect):
            for _ in range(3):
                compiler.reconcile_structural_alerts({'watch':{'TEST':{'resistance':100}}})
                compiler.reconcile_journal_alerts([dict(ticker='TEST',condition='above',threshold=100)])
                compiler.promote_recurring_journal_alerts(threshold=1)
        self.assertEqual(self.execute('SELECT count(*) FROM price_alerts',fetch=True)[0][0],0)
        self.assertEqual(self.execute("SELECT count(*) FROM alert_action_history WHERE action='purge'",fetch=True)[0][0],3)
        from alerts.management import save_manual
        self.assertIsInstance(save_manual('TEST','Explicit recreation','above',100),int)

    def test_stop_deletion_retains_purge_protection_and_historical_blocks(self):
        from alerts.management import apply_bulk,preview_action
        from morning_brief.alert_compiler import reconcile_structural_alerts
        self.execute("""INSERT INTO alert_purge_blocks(original_alert_id,source,identity,ticker,condition,threshold)
            VALUES (9999,'key_levels_watch','watch:OLD:support','OLD','below',50)""")
        stop=self.add_alert(source='key_levels_watch',source_key='position:TEST:stop',threshold=90)
        apply_bulk(preview_action([self.alert(stop)],'purge'),'purge')
        with patch('morning_brief.alert_compiler.get_connection',self.connect):
            stats=reconcile_structural_alerts({'positions':{'TEST':{'stop':90}}})
        self.assertEqual(stats['purged'],1)
        self.assertEqual(self.execute('SELECT count(*) FROM price_alerts',fetch=True)[0][0],0)
        self.assertEqual(self.execute('SELECT count(*) FROM alert_purge_blocks',fetch=True)[0][0],2)

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

    def test_inline_edits_preserve_identity_controls_and_source_refresh(self):
        from alerts.management import save_inline_edits,fingerprint
        from morning_brief.alert_compiler import reconcile_structural_alerts
        expiry=datetime.now(timezone.utc)+timedelta(days=3)
        alert_id=self.add_alert(source='key_levels_watch',source_key='watch:TEST:resistance',
                                threshold=100,triggered=True,expires_at=expiry)
        lifecycle.control_alert(alert_id,'pause')
        old=self.alert(alert_id)
        change=dict(id=alert_id,ticker='TEST',fingerprint=fingerprint(old),label='My level',
                    condition='below',threshold=95)
        save_inline_edits([change])
        new=self.alert(alert_id)
        self.assertEqual((new['source'],new['source_key']),(old['source'],old['source_key']))
        self.assertEqual((new['threshold'],new['condition'],new['paused'],new['expires_at']),(95,'below',True,expiry))
        self.assertFalse(new['triggered'])
        with patch('morning_brief.alert_compiler.get_connection',self.connect):
            reconcile_structural_alerts({'watch':{'TEST':{'resistance':120}}})
        self.assertEqual(new,self.alert(alert_id))
        with self.assertRaises(ValueError): save_inline_edits([change])
        change.update(fingerprint=fingerprint(new),remove_expiry=True)
        save_inline_edits([change])
        self.assertIsNone(self.alert(alert_id)['expires_at'])
        self.delivery.assert_not_called()

    def test_inline_batch_rolls_back_on_stop_or_duplicate(self):
        from alerts.management import save_inline_edits,fingerprint
        first=self.add_alert(threshold=100)
        second=self.add_alert(threshold=105)
        stop=self.add_alert(source='key_levels_watch',source_key='position:TEST:stop',threshold=90)
        def change(alert_id,level):
            return dict(id=alert_id,ticker='TEST',fingerprint=fingerprint(self.alert(alert_id)),
                        label='Edit',condition='above',threshold=level)
        with self.assertRaises(ValueError): save_inline_edits([change(first,102),change(stop,95)])
        self.assertEqual(self.alert(first)['threshold'],100)
        with self.assertRaises(ValueError): save_inline_edits([change(first,105)])
        self.assertEqual(self.alert(first)['threshold'],100)
        save_inline_edits([change(first,105)],allow_duplicate=True)
        self.assertEqual(self.alert(first)['threshold'],105)

    def test_browser_inline_edit_and_create_without_expiry(self):
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        alert_id=self.add_alert(source='key_levels_watch',source_key='watch:TEST:resistance')
        st.cache_data.clear()
        original=st.data_editor
        modify=[]
        def editor(*a,**kw):
            frame=original(*a,**kw)
            if modify:
                frame.loc[frame['ID']==alert_id,'Level']=107.0
            return frame
        with patch('dashboard.db.get_connection',self.connect),patch('streamlit.data_editor',side_effect=editor), \
             patch('dashboard.alerts_workspace.quote_snapshot') as quotes:
            app=AppTest.from_file(str(ROOT/'dashboard/pages/P11_Alerts.py'),default_timeout=20).run()
            self.assertFalse(app.exception)
            self.assertFalse(any('Expiry' in t.label for t in app.text_input))
            modify.append(True)
            app.button(key='workspace_save_edits').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(self.alert(alert_id)['threshold'],107)
            modify.clear()
            app.text_input(key='new_ticker').set_value('^VIX').run()
            app.number_input(key='new_threshold').set_value(18.0).run()
            next(b for b in app.button if b.label=='Create manual alert').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(self.execute("SELECT expires_at FROM price_alerts WHERE ticker='^VIX'",fetch=True),[(None,)])
            quotes.assert_not_called()

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
