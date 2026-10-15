"""Retirement migration and position-only persistence, in disposable test schemas."""
import os
import unittest
from unittest.mock import patch

import psycopg2
import test_alert_controls_db as fixtures


@unittest.skipUnless(os.environ.get('ALERT_TEST_DATABASE_URL'), 'Requires isolated PostgreSQL database')
class WatchLevelsRetirementDatabaseTests(unittest.TestCase):
    connect=fixtures.AlertControlsDatabaseTests.connect
    cleanup_schema=fixtures.AlertControlsDatabaseTests.cleanup_schema
    execute=fixtures.AlertControlsDatabaseTests.execute
    add_alert=fixtures.AlertControlsDatabaseTests.add_alert
    alert=fixtures.AlertControlsDatabaseTests.alert

    def setUp(self):
        fixtures.AlertControlsDatabaseTests.setUp(self)
        self.execute((fixtures.ROOT/'data_model/066_key_levels.sql').read_text())
        self.enterContext(patch('etrade_sync.db.get_connection',self.connect))
        self.migration=(fixtures.ROOT/'data_model/078_retire_saved_watch_levels.sql').read_text()

    def key_rows(self):
        return self.execute('SELECT row_to_json(k)::text FROM key_levels k ORDER BY id',fetch=True)

    def test_migration_twice_preserves_positions_alerts_controls_and_sequences(self):
        from alerts.lifecycle import control_alert
        self.execute("INSERT INTO key_levels(section,ticker,stop,note) VALUES ('positions','TEST',90,'Keep')")
        self.execute("INSERT INTO key_levels(section,ticker,support,resistance) VALUES ('watch','REF',80,110)")
        stop=self.add_alert(source='key_levels_watch',source_key='position:TEST:stop',threshold=90)
        watch=self.add_alert(source='key_levels_watch',source_key='watch:TEST:resistance')
        manual=self.add_alert()
        control_alert(watch,'pause')
        alerts={i:self.alert(i) for i in (stop,watch,manual)}
        positions=self.execute("SELECT id,section,ticker,stop,note,updated_at FROM key_levels WHERE section='positions'",fetch=True)
        sequence=self.execute('SELECT last_value,is_called FROM key_levels_id_seq',fetch=True)
        for _ in range(2):
            self.execute(self.migration)
            self.assertEqual(positions,self.execute('SELECT id,section,ticker,stop,note,updated_at FROM key_levels',fetch=True))
            self.assertEqual(alerts,{i:self.alert(i) for i in alerts})
            self.assertEqual(sequence,self.execute('SELECT last_value,is_called FROM key_levels_id_seq',fetch=True))
        with self.assertRaises(psycopg2.Error):
            self.execute("INSERT INTO key_levels(section,ticker) VALUES ('watch','NO')")
        with self.assertRaises(psycopg2.Error):
            self.execute('SELECT support FROM key_levels')
        self.delivery.assert_not_called()

    def test_unexpected_position_watch_data_aborts_without_partial_deletion(self):
        self.execute("INSERT INTO key_levels(section,ticker,support) VALUES ('positions','TEST',90),('watch','REF',80)")
        before=self.key_rows()
        with self.assertRaisesRegex(psycopg2.Error,'Position metadata contains'):
            self.execute(self.migration)
        self.assertEqual(before,self.key_rows())

    def test_unexpected_view_dependency_aborts_and_rolls_back_whole_migration(self):
        self.execute("INSERT INTO key_levels(section,ticker,support) VALUES ('watch','REF',80)")
        self.execute('CREATE VIEW saved_watch_dependency AS SELECT support FROM key_levels')
        before=self.key_rows()
        with self.assertRaises(psycopg2.Error):
            self.execute(self.migration)
        self.assertEqual(before,self.key_rows())
        self.assertEqual(self.execute('SELECT support FROM saved_watch_dependency',fetch=True)[0][0],80)

    def test_table_lock_timeout_leaves_data_unchanged(self):
        self.execute("INSERT INTO key_levels(section,ticker,support) VALUES ('watch','REF',80)")
        holder=self.connect()
        before=self.key_rows()
        try:
            with holder.cursor() as cur:
                cur.execute('LOCK TABLE key_levels IN ACCESS EXCLUSIVE MODE')
            with self.assertRaises(psycopg2.Error):
                self.execute(self.migration.replace("lock_timeout = '5s'", "lock_timeout = '100ms'"))
        finally:
            holder.rollback();holder.close()
        self.assertEqual(before,self.key_rows())

    def test_position_load_save_and_empty_table_seed_never_restore_watch_levels(self):
        from morning_brief.fetchers import load_key_levels_from_db,save_key_levels_to_db
        self.execute(self.migration)
        yaml='positions:\n  TEST:\n    stop: 90\n    note: Keep\nwatch:\n  RETIRED:\n    support: 50\n'
        with patch('pathlib.Path.read_text',return_value=yaml):
            levels=load_key_levels_from_db()
        self.assertEqual(levels,{'positions':{'TEST':{'stop':90,'note':'Keep'}}})
        original_id=self.execute('SELECT id FROM key_levels',fetch=True)[0][0]
        save_key_levels_to_db({'positions':{'TEST':{'stop':95,'note':'Updated'}}})
        self.assertEqual(original_id,self.execute('SELECT id FROM key_levels',fetch=True)[0][0])
        self.assertEqual(load_key_levels_from_db()['positions']['TEST']['stop'],95)
        before=self.key_rows()
        with self.assertRaisesRegex(ValueError,'retired'):
            save_key_levels_to_db({'watch':{'RETIRED':{'support':50}}})
        self.assertEqual(before,self.key_rows())

    def test_database_load_failure_is_visible_not_an_empty_success(self):
        from morning_brief.fetchers import load_key_levels_from_db
        with patch('etrade_sync.db.get_connection',side_effect=RuntimeError('DB unavailable')):
            with self.assertRaisesRegex(RuntimeError,'DB unavailable'):
                load_key_levels_from_db()

    def test_new_reader_and_writer_work_before_migration_without_deleting_legacy_references(self):
        from morning_brief.fetchers import load_key_levels_from_db,save_key_levels_to_db
        self.execute("INSERT INTO key_levels(section,ticker,support) VALUES ('watch','REF',80)")
        self.assertEqual(load_key_levels_from_db(),{'positions':{}})
        save_key_levels_to_db({'positions':{'TEST':{'stop':90}}})
        self.assertEqual(self.execute("SELECT support FROM key_levels WHERE section='watch'",fetch=True)[0][0],80)
        self.assertEqual(set(load_key_levels_from_db()['positions']),{'TEST'})

    def test_schema_refresh_preserves_no_expiry_alerts(self):
        from etrade_sync.db import _execute_schema_file
        alert_id=self.add_alert()
        before=self.alert(alert_id)
        c=self.connect()
        try:
            with c.cursor() as cur:
                _execute_schema_file(cur,fixtures.ROOT/'data_model/069_price_alerts_source_tier_expiry.sql')
            c.commit()
        finally:
            c.close()
        self.assertEqual(before,self.alert(alert_id))
        self.assertIsNone(self.alert(alert_id)['expires_at'])
