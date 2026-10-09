import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault('DATABASE_URL', 'postgresql://localhost/fake')

from alerts.lifecycle import identity, is_eligible, state, valid_price
from alerts.notify import deliver_alert_email
from alerts.poller import condition_met


class AlertControlsTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.alert = dict(id=1, enabled=True, archived_at=None, expires_at=None,
                          paused=False, snoozed_until=None, triggered=False,
                          threshold=100, condition='above')

    def test_each_suppression_excludes_alert(self):
        for field, value, expected in (
            ('enabled', False, 'Disabled'), ('paused', True, 'Paused'),
            ('archived_at', self.now, 'Archived'),
            ('expires_at', self.now, 'Expired'),
            ('snoozed_until', self.now + timedelta(hours=1), 'Snoozed')):
            alert = dict(self.alert, **{field: value})
            with self.subTest(state=expected):
                self.assertEqual(state(alert, self.now), expected)
                self.assertFalse(is_eligible(alert, self.now))

    def test_snooze_expiry_and_triggered_are_eligible(self):
        alert = dict(self.alert, snoozed_until=self.now, triggered=True)
        self.assertTrue(is_eligible(alert, self.now))
        self.assertEqual(state(alert, self.now), 'Condition met')

    def test_stable_managed_identity_and_legacy_row_fallback(self):
        self.assertEqual(identity(dict(id=3, source_key='watch:XLE:support')), 'watch:XLE:support')
        self.assertEqual(identity(dict(id=3, source_key=None)), 'row:3')

    def test_invalid_quotes_never_trigger(self):
        for price in (None, 0, -1, float('nan'), float('inf'), 'invalid'):
            self.assertFalse(valid_price(price))
            self.assertFalse(condition_met(self.alert, price))

    def test_strict_comparison_and_unknown_condition(self):
        self.assertFalse(condition_met(self.alert, 100))
        self.assertTrue(condition_met(self.alert, 101))
        self.assertTrue(condition_met(dict(self.alert, condition='below'), 99))
        self.assertFalse(condition_met(dict(self.alert, condition='invalid'), 101))

    @patch.dict(os.environ, {}, clear=True)
    def test_unconfigured_delivery_is_distinct(self):
        self.assertEqual(deliver_alert_email('TEST', '', 'above', 100, 101).status, 'not_configured')

    @patch.dict(os.environ, {'ALERT_SMTP_HOST': 'localhost', 'ALERT_SMTP_USER': 'u',
                            'ALERT_SMTP_PASS': 'secret', 'ALERT_EMAIL_TO': 't'}, clear=True)
    @patch('alerts.notify.smtplib.SMTP')
    def test_email_success_uses_timeout(self, smtp):
        self.assertEqual(deliver_alert_email('TEST', '', 'above', 100, 101).status, 'sent')
        smtp.assert_called_once_with('localhost', 587, timeout=20)

    @patch.dict(os.environ, {'ALERT_SMTP_HOST': 'localhost', 'ALERT_SMTP_USER': 'u',
                            'ALERT_SMTP_PASS': 'secret', 'ALERT_EMAIL_TO': 't'}, clear=True)
    @patch('alerts.notify.smtplib.SMTP', side_effect=RuntimeError('secret'))
    def test_delivery_failure_does_not_retain_secrets(self, smtp):
        result = deliver_alert_email('TEST', '', 'above', 100, 101)
        self.assertEqual(result.status, 'failed')
        self.assertNotIn('secret', result.error)


if __name__ == '__main__':
    unittest.main()
