import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault('DATABASE_URL', 'postgresql://localhost/fake')
from dashboard.alerts_workspace import page_notifications


class PageNotificationsTests(unittest.TestCase):
    def test_conditions_controls_and_freshness(self):
        now = datetime.now(timezone.utc)
        alert = dict(id=1, ticker='TEST', condition='above', threshold=100,
                     enabled=True, archived_at=None, paused=False)
        quote = dict(price=101, retrieved_at=now)
        self.assertEqual(len(page_notifications([alert], {'TEST': quote}, now)), 1)
        for change in (dict(paused=True), dict(enabled=False), dict(archived_at=now),
                       dict(expires_at=now), dict(snoozed_until=now+timedelta(hours=1)),
                       dict(threshold=101), dict(condition='below')):
            with self.subTest(change=change):
                self.assertEqual(page_notifications([dict(alert, **change)], {'TEST': quote}, now), [])
        for change in (dict(price=None), dict(price=float('nan')), dict(error='Unavailable'),
                       dict(retrieved_at=now-timedelta(minutes=11))):
            with self.subTest(change=change):
                self.assertEqual(page_notifications([alert], {'TEST': dict(quote, **change)}, now), [])
        self.assertEqual(page_notifications([alert], {}, now), [])
        self.assertEqual(len(page_notifications([dict(alert, condition='below')],
                         {'TEST': dict(quote, price=99)}, now)), 1)
