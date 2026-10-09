import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql://localhost/fake')
from alerts.hygiene import review_exceptions
from dashboard.alerts_workspace import split_snapshot


class AlertHygieneTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        self.alert = dict(id=1, ticker='TEST', source='manual', threshold=100,
            condition='above', enabled=True, triggered=False, archived_at=None,
            expires_at=None, paused=False, snoozed_until=None, created_at=self.now)
        self.quote = dict(price=100, retrieved_at=self.now)

    def review(self, alerts=None, quotes=None, splits=None, **kwargs):
        return review_exceptions(alerts if alerts is not None else [self.alert],
            {'TEST': self.quote} if quotes is None else quotes, splits, now=self.now, **kwargs)

    def test_normal_level_produces_no_finding_and_input_unchanged(self):
        before = dict(self.alert)
        self.assertEqual(self.review(), [])
        self.assertEqual(before, self.alert)

    def test_expired_and_archived_handling(self):
        expired = dict(self.alert, expires_at=self.now-timedelta(seconds=1))
        self.assertIn('Expired idea', self.review([expired])[0]['Reasons'])
        archived = dict(expired, archived_at=self.now)
        self.assertEqual(self.review([archived]), [])
        self.assertEqual(len(self.review([archived], include_archived=True)), 1)

    def test_age_ignores_compiler_refresh_and_uses_journal_evidence(self):
        old = dict(self.alert, created_at=self.now-timedelta(days=91), refreshed_at=self.now)
        self.assertIn('91 days', self.review([old])[0]['Reasons'])
        journal = dict(old, source='journal_sync', last_seen_at=self.now)
        self.assertEqual(self.review([journal]), [])

    def test_distance_boundary_missing_and_stale_quotes(self):
        distant = dict(self.alert, threshold=125)
        self.assertIn('25.0%', self.review([distant])[0]['Reasons'])
        for quotes in ({}, {'TEST': dict(self.quote, retrieved_at=self.now-timedelta(minutes=11))}):
            row = self.review([distant], quotes)[0]
            self.assertIsNone(row['Distance %'])
            self.assertNotIn('Large price distance', row['Reasons'])

    def test_invalid_level_is_high_priority_and_does_not_crash(self):
        for value in (0, -1, float('nan'), float('inf')):
            row = self.review([dict(self.alert, threshold=value)])[0]
            self.assertEqual(row['Priority'], 1)
            self.assertIn('Invalid stored level', row['Reasons'])

    def test_split_evidence_must_postdate_recorded_level(self):
        old = dict(self.alert, threshold=1000, created_at=self.now-timedelta(days=30))
        events = {'TEST': dict(events=[dict(date=date(2026, 10, 1), ratio=10)])}
        self.assertIn('Possible split mismatch', self.review([old], splits=events)[0]['Reasons'])
        self.assertNotIn('Possible split mismatch', self.review([dict(old, created_at=self.now)], splits=events)[0]['Reasons'])
        row = self.review([old], splits={'TEST': dict(error='Unavailable')})[0]
        self.assertIn('no split conclusion', row['Reasons'])

    def test_multiple_and_reverse_splits(self):
        old = dict(self.alert, threshold=1000, created_at=self.now-timedelta(days=30))
        events = {'TEST': dict(events=[dict(date=date(2026, 10, 1), ratio=2),
                                      dict(date=date(2026, 10, 2), ratio=5)])}
        self.assertIn('cumulative ratio 10', self.review([old], splits=events)[0]['Reasons'])
        reverse = {'TEST': dict(events=[dict(date=date(2026, 10, 1), ratio=0.1)])}
        self.assertIn('Possible split mismatch', self.review([dict(old, threshold=10)], splits=reverse)[0]['Reasons'])

    def test_split_provider_failure_is_reported_without_mutation(self):
        split_snapshot.clear()
        with patch('dashboard.alerts_workspace.yf.Ticker', side_effect=RuntimeError('offline')):
            self.assertEqual(split_snapshot(('TEST',))['TEST']['error'], 'RuntimeError')
        split_snapshot.clear()

    def test_split_provider_preserves_action_dates_and_ratios(self):
        import pandas as pd
        ticker = MagicMock()
        ticker.history.return_value = pd.DataFrame({'Stock Splits': [0, 10]},
            index=pd.to_datetime(['2026-09-30', '2026-10-01']))
        split_snapshot.clear()
        with patch('dashboard.alerts_workspace.yf.Ticker', return_value=ticker):
            data = split_snapshot(('TEST',))['TEST']
        self.assertEqual(data['events'], [dict(date=date(2026, 10, 1), ratio=10)])
        ticker.history.assert_called_once_with(period='2y', auto_adjust=False, actions=True)
        split_snapshot.clear()
