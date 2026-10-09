import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault('DATABASE_URL', 'postgresql://localhost/fake')
from alerts.management import fingerprint, preview_action, _manual_values
from dashboard.alerts_workspace import distance_percent, filter_alerts, selected_alerts, attention_reasons


class AlertManagementTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.alert = dict(id=1, ticker='XLE', label='Energy support', condition='below', threshold=100.0,
            source='manual', source_key=None, enabled=True, archived_at=None, expires_at=None,
            paused=False, snoozed_until=None, triggered=False, created_at=self.now)
        self.quote = dict(price=102.0, retrieved_at=self.now)

    def test_distance_handles_missing_and_invalid_prices(self):
        self.assertAlmostEqual(distance_percent(self.alert, self.quote), 2 / 102 * 100)
        for price in (None, 0, float('nan'), -1):
            self.assertIsNone(distance_percent(self.alert, dict(price=price)))

    def test_large_distances_can_be_filtered_for_purge(self):
        far = dict(self.alert, threshold=900)
        quotes = {'XLE': dict(self.quote, price=100)}
        self.assertEqual(distance_percent(far, quotes['XLE']), 800)
        self.assertEqual(filter_alerts([self.alert, far], quotes, minimum_distance=100), [far])
        self.assertEqual(filter_alerts([far], {}, minimum_distance=100), [])

    def test_filters_combine_without_selecting_hidden_rows(self):
        other = dict(self.alert, id=2, ticker='^VIX', source='key_levels_watch', condition='above')
        self.assertEqual(filter_alerts([other, self.alert], {}, search='energy', sources=['manual'],
                                      states=['Armed'], condition='below'), [self.alert])
        self.assertEqual(filter_alerts([self.alert], {}, trigger='Condition met'), [])

    def test_history_and_attention_views(self):
        archived = dict(self.alert, id=2, archived_at=self.now)
        self.assertEqual(filter_alerts([self.alert, archived], {}, view='History'), [archived])
        self.assertIn('Quote missing', attention_reasons(self.alert, None))
        stale_quote = dict(self.quote, retrieved_at=self.now - timedelta(minutes=11))
        self.assertIn('Snapshot needs refresh', attention_reasons(self.alert, stale_quote))

    def test_selection_resolves_exact_ids_and_rejects_stale_index(self):
        rows = [dict(self.alert, id=9), dict(self.alert, id=3)]
        self.assertEqual([a['id'] for a in selected_alerts(rows, [1])], [3])
        with self.assertRaises(ValueError):
            selected_alerts(rows, [2])

    def test_preview_blocks_mixed_managed_archive(self):
        managed = dict(self.alert, id=2, source='journal_sync')
        preview = preview_action([self.alert, managed], 'archive')
        self.assertIsNone(preview[0]['error'])
        self.assertIsNotNone(preview[1]['error'])

    def test_fingerprint_rejects_config_changes_but_not_poller_updates(self):
        self.assertEqual(fingerprint(self.alert), fingerprint(dict(self.alert, triggered=True, delivery_status='sent')))
        self.assertNotEqual(fingerprint(self.alert), fingerprint(dict(self.alert, threshold=101)))
        self.assertNotEqual(fingerprint(self.alert), fingerprint(dict(self.alert, paused=True)))

    def test_manual_validation_and_timezone(self):
        self.assertEqual(_manual_values(' nq=f ', ' note ', 'above', 100, None)[:2], ('NQ=F', 'note'))
        for kwargs in [('bad ticker', 'above', 100, None), ('XLE', 'invalid', 100, None),
                       ('XLE', 'above', float('inf'), None), ('XLE', 'above', 100, self.now.replace(tzinfo=None))]:
            with self.assertRaises(ValueError):
                _manual_values(kwargs[0], '', *kwargs[1:])


if __name__ == '__main__':
    unittest.main()
