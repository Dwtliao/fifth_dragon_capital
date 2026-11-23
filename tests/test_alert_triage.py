import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault('DATABASE_URL', 'postgresql://localhost/fake')
from dashboard.alert_triage import BANDS, apply_plan, plan_changes, remove_unheld_stops, triage_rows

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def _alert(id, ticker, threshold, condition='below', source='key_levels_watch', source_key=None, **kw):
    alert = dict(id=id, ticker=ticker, label=f'{ticker} level', condition=condition, threshold=threshold,
                 source=source, source_key=source_key or f'watch:{ticker}:{id}', enabled=True, archived_at=None,
                 expires_at=None, paused=False, snoozed_until=None, triggered=False, created_at=NOW)
    alert.update(kw)
    return alert


def _q(price):
    return dict(price=price, error=None, retrieved_at=NOW)


ALERTS = [
    _alert(1, 'AAA', 98.0),                                   # 2% -> Near
    _alert(2, 'BBB', 80.0),                                   # 20% -> Medium
    _alert(3, 'CCC', 300.0, condition='above'),              # 200% -> Far, very far
    _alert(4, 'AAA', 50.0, source_key='position:AAA:stop'),  # 49% -> Far, stop on held ticker
    _alert(5, 'NOQ', 10.0),                                   # no price
    _alert(6, 'OLD', 10.0, archived_at=NOW),                  # inactive -> excluded
    _alert(7, 'GONE', 5.0, source_key='position:GONE:stop'),  # stop on unheld ticker, no price
]
QUOTES = {'AAA': _q(100.0), 'BBB': _q(100.0), 'CCC': _q(100.0)}
HELD = {'AAA', 'BBB'}


class TriageRowsTests(unittest.TestCase):
    def setUp(self):
        self.rows = triage_rows(ALERTS, QUOTES, HELD, now=NOW)

    def test_only_active_alerts_sorted_by_band_then_distance(self):
        self.assertEqual([r['id'] for r in self.rows], [1, 2, 4, 3, 5, 7])
        self.assertEqual([r['band'] for r in self.rows],
                         [BANDS[0], BANDS[1], BANDS[2], BANDS[2], BANDS[3], BANDS[3]])

    def test_flags(self):
        by_id = {r['id']: r for r in self.rows}
        self.assertIn('Stop, held — edit in P10', by_id[4]['flags'])
        self.assertIn('Stop, not held — Delete removes the stop', by_id[7]['flags'])
        self.assertIn('2 alerts on ticker', by_id[1]['flags'])
        self.assertIn('Very far', by_id[3]['flags'])
        self.assertEqual(by_id[2]['flags'], '')


def _rec(alert_id, decision='Keep', new_level=None):
    level = new_level if new_level is not None else next(a['threshold'] for a in ALERTS if a['id'] == alert_id)
    return {'ID': alert_id, 'Decision': decision, 'New level': level}


class PlanChangesTests(unittest.TestCase):
    by_id = {a['id']: a for a in ALERTS}

    def plan(self, records):
        return plan_changes(self.by_id, records, HELD)

    def test_unchanged_keep_rows_are_a_no_op(self):
        self.assertEqual(self.plan([_rec(1), _rec(2), _rec(4)]), ([], [], []))

    def test_changed_level_is_an_edit_and_delete_wins(self):
        deletes, edits, stops = self.plan([_rec(2, 'Delete', 70.0), _rec(3, 'Keep', 120.0), _rec(1)])
        self.assertEqual([a['id'] for a in deletes], [2])
        self.assertEqual([(e['id'], e['threshold'], e['condition']) for e in edits], [(3, 120.0, 'above')])
        self.assertFalse(edits[0]['remove_expiry'])
        self.assertEqual(stops, [])

    def test_level_typed_as_text_is_accepted(self):
        _, edits, _ = self.plan([{'ID': 3, 'Decision': 'Keep', 'New level': '190'}])
        self.assertEqual(edits[0]['threshold'], 190.0)

    def test_delete_unheld_stop_removes_the_stop(self):
        self.assertEqual(self.plan([_rec(7, 'Delete')]), ([], [], ['GONE']))

    def test_held_stop_cannot_be_deleted_or_moved(self):
        with self.assertRaisesRegex(ValueError, 'stop on a stock you hold'):
            self.plan([_rec(4, 'Delete')])
        with self.assertRaisesRegex(ValueError, 'position stop'):
            self.plan([_rec(4, 'Keep', 60.0)])

    def test_invalid_new_level_is_rejected(self):
        for level in (0, -1, float('nan'), 'abc'):
            with self.assertRaisesRegex(ValueError, 'greater than zero'):
                self.plan([{'ID': 2, 'Decision': 'Keep', 'New level': level}])

    def test_unknown_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'reload'):
            self.plan([{'ID': 999, 'Decision': 'Delete', 'New level': 1.0}])


class ApplyPlanTests(unittest.TestCase):
    def test_runs_edits_deletes_and_stop_removals_with_existing_services(self):
        with patch('dashboard.alert_triage.save_inline_edits', return_value=[3]) as edit, \
             patch('dashboard.alert_triage.apply_bulk', return_value=[2]) as bulk, \
             patch('dashboard.alert_triage.remove_unheld_stops', return_value=['GONE']) as stops:
            message = apply_plan([ALERTS[1]], [dict(id=3)], ['GONE'])
        edit.assert_called_once()
        self.assertEqual(bulk.call_args.args[1], 'purge')
        stops.assert_called_once_with(['GONE'])
        for text in ('edited levels for IDs [3]', 'deleted IDs [2]', 'removed stops for GONE'):
            self.assertIn(text, message)

    def test_failure_reports_what_was_already_saved(self):
        with patch('dashboard.alert_triage.save_inline_edits', return_value=[3]), \
             patch('dashboard.alert_triage.apply_bulk', side_effect=ValueError('changed since preview')):
            with self.assertRaisesRegex(ValueError, 'Already saved: edited levels'):
                apply_plan([ALERTS[1]], [dict(id=3)], [])

    def test_nothing_to_do(self):
        self.assertIn('every alert was kept', apply_plan([], [], []))


class RemoveUnheldStopsTests(unittest.TestCase):
    def test_removes_only_named_tickers_using_the_p10_save_path(self):
        baseline = {'positions': {'GONE': {'stop': 5.0, 'note': 'old'}, 'AAA': {'stop': 50.0}}}
        with patch('morning_brief.fetchers.load_key_levels_from_db', return_value=baseline), \
             patch('morning_brief.alert_compiler.save_structural_key_levels', return_value={'archived': 1}) as save:
            self.assertEqual(remove_unheld_stops(['gone']), ['GONE'])
        updated, passed_baseline = save.call_args.args
        self.assertEqual(updated, {'positions': {'AAA': {'stop': 50.0}}})
        self.assertIn('GONE', passed_baseline['positions'])  # baseline left intact for the CAS check

    def test_alert_refresh_failure_is_reported(self):
        with patch('morning_brief.fetchers.load_key_levels_from_db', return_value={'positions': {'GONE': {'stop': 5.0}}}), \
             patch('morning_brief.alert_compiler.save_structural_key_levels', return_value={'error': 'boom'}):
            with self.assertRaisesRegex(ValueError, 'alert refresh failed'):
                remove_unheld_stops(['GONE'])


if __name__ == '__main__':
    unittest.main()
