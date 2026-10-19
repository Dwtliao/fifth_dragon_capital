"""Review regressions with mocked persistence; no production database or network."""
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql://localhost/fake')


class ReviewGuardTests(unittest.TestCase):
    def test_empty_position_snapshots_never_connect(self):
        from morning_brief.fetchers import save_key_levels_to_db
        for snapshot in ({}, {'positions': {}}, {'positions': None}):
            with self.subTest(snapshot=snapshot), patch('etrade_sync.db.get_connection') as connect:
                with self.assertRaisesRegex(ValueError, 'empty positions'):
                    save_key_levels_to_db(snapshot)
                connect.assert_not_called()

    def test_consolidation_requires_exact_position_stop(self):
        from alerts.management import consolidation_preview
        base = dict(ticker='TEST', condition='below', threshold=100, enabled=True,
                    archived_at=None, paused=False)
        for source in ('manual', 'journal_sync'):
            candidate = dict(base, id=1, source=source, source_key=None)
            legacy = dict(base, id=2, source='key_levels_watch', source_key='watch:TEST:support')
            with self.subTest(source=source):
                self.assertIsNone(consolidation_preview([candidate, legacy]))
                stop = dict(legacy, source_key='position:TEST:stop')
                self.assertIsNotNone(consolidation_preview([candidate, stop]))

    def test_expiry_backfill_missing_marker_fails_before_mutating_sql(self):
        from etrade_sync.db import _execute_schema_file
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        with patch.object(Path, 'read_text', return_value='UPDATE price_alerts SET expires_at=NOW();'):
            with self.assertRaisesRegex(RuntimeError, 'refusing to risk replaying'):
                _execute_schema_file(cursor, Path('069_price_alerts_source_tier_expiry.sql'))
        self.assertEqual(cursor.execute.call_count, 1)  # Metadata SELECT only.
        self.assertTrue(cursor.execute.call_args.args[0].startswith('SELECT EXISTS'))

    def test_existing_expiry_column_skips_only_backfill(self):
        from etrade_sync.db import _execute_schema_file
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        with patch.object(Path, 'read_text', return_value='SELECT 1;\n-- Back-fill:\nUPDATE price_alerts SET expires_at=NOW();'):
            _execute_schema_file(cursor, Path('069_price_alerts_source_tier_expiry.sql'))
        self.assertEqual(cursor.execute.call_args.args[0], 'SELECT 1;\n')
