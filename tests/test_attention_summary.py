import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql://localhost/fake')
from morning_brief.attention import attention_items, render_attention, attention_summary


class AttentionSummaryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        self.alert = dict(id=46, ticker='UUUU', source='key_levels_watch',
            source_key='position:UUUU:stop', threshold=14, condition='below',
            enabled=True, triggered=True, archived_at=None, paused=False,
            expires_at=None, snoozed_until=None, created_at=self.now)
        self.quotes = {'UUUU': dict(last=12, as_of='2026-10-09')}

    def groups(self, alert=None, held=None, quotes=None):
        return attention_items([alert or self.alert], held or set(),
                               self.quotes if quotes is None else quotes, self.now)

    def test_closed_position_stop_is_ambiguity_not_buy_signal(self):
        groups = self.groups()
        self.assertEqual(len(groups['Resolve ambiguity']), 1)
        self.assertEqual(groups['Watch opportunities'], [])
        output = render_attention(groups)
        self.assertIn('re-entry watch, update, or retire', output)
        self.assertIn('#46', output)
        self.assertIn('not a buy signal', output)

    def test_held_stop_met_and_nearby_protect_holdings(self):
        self.assertEqual(len(self.groups(held={'UUUU'})['Protect holdings']), 1)
        self.assertIn(':red[🔴 Held stop breached]', render_attention(self.groups(held={'UUUU'})))
        near = {'UUUU': dict(last=14.1, as_of='2026-10-09')}
        row = self.groups(held={'UUUU'}, quotes=near)['Protect holdings'][0]
        self.assertEqual(row['condition'], 'not met at displayed daily bar')

    def test_unheld_watch_crossing_is_opportunity_not_stop(self):
        watch = dict(self.alert, source_key='watch:UUUU:support')
        self.assertEqual(len(self.groups(watch)['Watch opportunities']), 1)
        output = render_attention(self.groups(watch))
        self.assertIn(':green[🟢 Watch condition met', output)
        self.assertIn('Alert: **below 14.00**', output)
        self.assertIn('Price snapshot: **12.00**', output)

    def test_unmet_watch_is_neutral_but_review_reason_is_amber(self):
        watch = dict(self.alert, source_key='watch:UUUU:resistance',
                     condition='above', threshold=73, created_at=self.now-timedelta(days=100))
        output = render_attention(self.groups(watch), generated_at=self.now)
        self.assertIn(':gray[⚪ Condition not met]', output)
        self.assertIn(':orange[🟡 Review needed]', output)
        self.assertIn('Snapshot retrieved 2026-10-09 12:00:00 UTC', output)

    def test_suppressed_met_condition_is_not_colored_as_opportunity(self):
        alert = dict(self.alert, source_key='watch:UUUU:support', paused=True,
                     created_at=self.now-timedelta(days=100))
        output = render_attention(self.groups(alert))
        self.assertIn(':gray[⚪ Paused', output)
        self.assertNotIn(':green[', output)

    def test_stale_or_missing_price_never_claims_current_condition_met(self):
        for quotes in ({}, {'UUUU': dict(last=12, as_of='2026-09-30')}):
            row = self.groups(quotes=quotes)['Resolve ambiguity'][0]
            self.assertEqual(row['condition'], 'unknown')
            self.assertEqual(row['last_poll'], 'met')

    def test_paused_and_expired_watch_are_not_active_opportunities(self):
        watch = dict(self.alert, source_key='watch:UUUU:support')
        self.assertEqual(self.groups(dict(watch, paused=True))['Watch opportunities'], [])
        expired = dict(watch, expires_at=self.now-timedelta(days=1))
        self.assertEqual(len(self.groups(expired)['Resolve ambiguity']), 1)

    def test_archived_is_excluded_and_visible_list_is_capped(self):
        self.assertFalse(any(self.groups(dict(self.alert, archived_at=self.now)).values()))
        alerts = [dict(self.alert, id=i) for i in range(8)]
        groups = attention_items(alerts, set(), self.quotes, self.now)
        output = render_attention(groups)
        self.assertIn('Showing 5 of 8', output)
        self.assertNotIn('#7', output)

    def test_current_price_and_last_poll_disagreement_is_visible(self):
        watch = dict(self.alert, source_key='watch:UUUU:support')
        row = self.groups(watch, quotes={'UUUU': dict(last=15, as_of='2026-10-09')})['Resolve ambiguity'][0]
        self.assertEqual(row['last_poll'], 'met')
        self.assertEqual(row['condition'], 'not met at displayed daily bar')

    def test_loader_uses_read_only_connection_and_no_mutations(self):
        conn = MagicMock()
        with patch('morning_brief.attention.fetch_positions_from_db', return_value=[]), \
             patch('morning_brief.attention.get_connection', return_value=conn), \
             patch('morning_brief.attention.load_alerts', return_value=[self.alert]), \
             patch('morning_brief.attention._fetch_snapshot', return_value=[
                 dict(ticker='UUUU', last=12, as_of='2026-10-09')]):
            output = attention_summary()
        self.assertIn('#46', output)
        conn.set_session.assert_called_once_with(readonly=True)
        conn.commit.assert_not_called()
        conn.close.assert_called_once()

    def test_holdings_failure_does_not_infer_unheld_opportunities(self):
        with patch('morning_brief.attention.fetch_positions_from_db', return_value=[{'error':'offline'}]), \
             patch('morning_brief.attention.get_connection') as connect:
            with self.assertRaises(RuntimeError):
                attention_summary()
            connect.assert_not_called()
