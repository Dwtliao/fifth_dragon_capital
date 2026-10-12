"""Approval workflow fixtures use only the opt-in isolated database."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import test_alert_controls_db as fixtures
from morning_brief import journal_review as review
from alerts import lifecycle


@unittest.skipUnless(os.environ.get('ALERT_TEST_DATABASE_URL'),'Requires isolated PostgreSQL database')
class JournalReviewDatabaseTests(unittest.TestCase):
    connect=fixtures.AlertControlsDatabaseTests.connect
    cleanup_schema=fixtures.AlertControlsDatabaseTests.cleanup_schema
    execute=fixtures.AlertControlsDatabaseTests.execute
    add_alert=fixtures.AlertControlsDatabaseTests.add_alert
    alert=fixtures.AlertControlsDatabaseTests.alert

    def setUp(self):
        fixtures.AlertControlsDatabaseTests.setUp(self)
        for file in ('066_key_levels.sql','067_journal_sync_log.sql','077_journal_review.sql'):
            self.execute((fixtures.ROOT/'data_model'/file).read_text())
        self.execute('CREATE TABLE positions (account_id_key TEXT,symbol TEXT,security_type TEXT,quantity NUMERIC,fetched_at TIMESTAMPTZ)')
        self.enterContext(patch('morning_brief.journal_review.get_connection',self.connect))

    def stage(self, threshold=110, ticker='TEST', stop=False, evidence=None,timeframe='price_crossing'):
        evidence=evidence or f'Alert me if {ticker} is below {threshold}.'
        raw=dict(ticker=ticker,evidence=evidence,timeframe=timeframe)
        extracted={'positions':[dict(raw,stop=threshold)]} if stop else {'price_alerts':[dict(raw,condition='below',threshold=threshold)]}
        review.stage_extraction(extracted,evidence,'journal_2026-10-09.md')
        return next(i for i in review.list_items() if float(i['threshold'] or 0)==threshold and i['ticker']==ticker)

    def approve(self,item,options=None,**kw):
        return review.review_action(review.preview_selection([item],{item['id']:options or {}}),'approve',acknowledge_market=True,**kw)

    def test_staging_never_changes_live_alerts_or_levels(self):
        existing=self.add_alert()
        before=self.alert(existing)
        self.execute("INSERT INTO key_levels(section,ticker,stop) VALUES ('positions','TEST',90)")
        self.stage(110)
        self.stage(80,stop=True)
        self.assertEqual(before,self.alert(existing))
        self.assertEqual(self.execute('SELECT stop FROM key_levels',fetch=True)[0][0],90)
        self.assertEqual(self.execute('SELECT count(*) FROM price_alerts',fetch=True)[0][0],1)
        self.delivery.assert_not_called()

    def test_accepted_rejected_repeats_do_not_create_or_renew(self):
        first=self.stage()
        self.approve(first)
        accepted=next(i for i in review.list_items() if i['id']==first['id'])
        before=self.alert(accepted['alert_id'])
        self.stage()
        self.assertEqual(before,self.alert(accepted['alert_id']))
        second=self.stage(120)
        review.review_action(review.preview_selection([second]),'reject')
        repeated=self.stage(120)
        self.assertEqual(repeated['status'],'rejected')
        self.assertEqual(self.execute('SELECT count(*) FROM price_alerts',fetch=True)[0][0],1)
        self.assertEqual(self.stage(130)['status'],'pending')

    def test_manual_approval_default_expiry_and_no_email(self):
        self.approve(self.stage())
        alert=self.alert(review.list_items()[0]['alert_id'])
        self.assertEqual(alert['source'],'manual')
        self.assertAlmostEqual((alert['expires_at']-datetime.now(timezone.utc)).total_seconds()/86400,21,places=2)
        self.delivery.assert_not_called()

    def test_watch_idea_requires_preparation_and_timeframe_clarification(self):
        item=self.stage(evidence='TEST sustained break below 110.',timeframe='sustained')
        self.assertEqual(item['status'],'idea')
        with self.assertRaises(ValueError): self.approve(item)
        review.review_action(review.preview_selection([item]),'prepare')
        item=review.list_items()[0]
        with self.assertRaises(ValueError): self.approve(item)
        self.approve(item,{'clarified_price_crossing':True})
        self.assertEqual(review.list_items()[0]['status'],'accepted')

    def test_stale_preview_batch_rolls_back_without_partial_alerts(self):
        a,b=self.stage(110),self.stage(120)
        preview=review.preview_selection([a,b])
        self.execute('UPDATE journal_review_items SET revision=revision+1 WHERE id=%s',(b['id'],))
        with self.assertRaises(ValueError):
            review.review_action(preview,'approve',acknowledge_market=True)
        self.assertEqual(self.execute('SELECT count(*) FROM price_alerts',fetch=True)[0][0],0)
        self.assertEqual(self.execute('SELECT count(*) FROM journal_review_decisions',fetch=True)[0][0],0)

    def test_replace_preserves_pause_and_rejects_changed_target(self):
        target=self.add_alert()
        lifecycle.control_alert(target,'pause')
        item=self.stage()
        preview=review.preview_selection([item],{item['id']:dict(mode='replace',target_id=target)})
        self.execute('UPDATE price_alerts SET threshold=101 WHERE id=%s',(target,))
        with self.assertRaises(ValueError): review.review_action(preview,'approve',acknowledge_market=True)
        self.approve(item,dict(mode='replace',target_id=target))
        self.assertTrue(self.alert(target)['paused'])
        self.assertFalse(lifecycle.is_eligible(self.alert(target)))

    def test_exact_duplicate_keep_existing_is_no_op(self):
        existing=self.add_alert(threshold=110)
        self.execute("UPDATE price_alerts SET condition='below' WHERE id=%s",(existing,))
        before=self.alert(existing)
        item=self.stage()
        with self.assertRaises(ValueError): self.approve(item)
        self.approve(item,dict(mode='keep_existing',target_id=existing))
        self.assertEqual(before,self.alert(existing))

    def test_holding_stop_approval_checks_ownership_and_preserves_controls(self):
        item=self.stage(90,stop=True,evidence='TEST stop 90.')
        with self.assertRaises(ValueError): self.approve(item)
        self.execute("INSERT INTO positions VALUES ('account','TEST','EQ',2,NOW())")
        self.approve(item)
        alert_id=review.list_items()[0]['alert_id']
        self.assertEqual(self.alert(alert_id)['source_key'],'position:TEST:stop')
        lifecycle.control_alert(alert_id,'pause')
        changed=self.stage(95,stop=True,evidence='TEST stop 95.')
        self.approve(changed)
        self.assertTrue(self.alert(alert_id)['paused'])
        self.assertEqual(self.execute('SELECT stop FROM key_levels',fetch=True)[0][0],95)

    def test_purged_stop_cannot_be_silently_restored(self):
        from alerts.management import apply_bulk,preview_action
        self.execute("INSERT INTO positions VALUES ('account','TEST','EQ',2,NOW())")
        old=self.add_alert(source='key_levels_watch',source_key='position:TEST:stop',threshold=90)
        apply_bulk(preview_action([self.alert(old)],'purge'),'purge')
        with self.assertRaises(ValueError): self.approve(self.stage(95,stop=True,evidence='TEST stop 95.'))
        self.assertEqual(self.execute('SELECT count(*) FROM key_levels',fetch=True)[0][0],0)

    def test_stale_holdings_block_stop_approval(self):
        self.execute("INSERT INTO positions VALUES ('account','TEST','EQ',2,NOW()-INTERVAL '10 days')")
        with self.assertRaises(ValueError): self.approve(self.stage(90,stop=True,evidence='TEST stop 90.'))

    def test_repeat_migration_leaves_live_state_unchanged(self):
        existing=self.add_alert()
        before=self.alert(existing)
        self.execute((fixtures.ROOT/'data_model/077_journal_review.sql').read_text())
        self.assertEqual(before,self.alert(existing))

    def test_browser_approval_requires_selection_preview_and_two_confirmations(self):
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        item=self.stage()
        st.cache_data.clear()
        with patch('dashboard.db.get_connection',self.connect), \
             patch('dashboard.journal_review_ui.quote_snapshot') as quotes:
            app=AppTest.from_string('from dashboard.journal_review_ui import render_journal_review\nrender_journal_review()',default_timeout=20).run()
            self.assertEqual(len(app.exception),0)
            self.assertFalse(any(c.value for c in app.checkbox if c.label.startswith('Select idea')))
            quotes.assert_not_called()
            next(c for c in app.checkbox if c.label.startswith('Select idea')).check().run()
            app.button(key='jr_preview_button').click().run()
            self.assertTrue(app.button(key='jr_apply').disabled)
            app.button(key='jr_cancel').click().run()
            self.assertEqual(self.execute('SELECT count(*) FROM price_alerts',fetch=True)[0][0],0)
            app.button(key='jr_preview_button').click().run()
            next(c for c in app.checkbox if c.label.startswith('Confirm this action')).check().run()
            self.assertTrue(app.button(key='jr_apply').disabled)
            next(c for c in app.checkbox if c.label.startswith('I reviewed evidence')).check().run()
            app.button(key='jr_apply').click().run()
            self.assertEqual(len(app.exception),0)
            self.assertEqual(review.list_items()[0]['status'],'accepted')
            self.assertEqual(self.execute('SELECT count(*) FROM price_alerts',fetch=True)[0][0],1)
            quotes.assert_not_called()
        self.delivery.assert_not_called()

    def test_blocked_and_invalid_batch_never_partially_activates(self):
        valid=self.stage(110)
        bad=self.stage(120,evidence='TEST mentioned but no level stated.')
        with self.assertRaises(ValueError):
            review.review_action(review.preview_selection([valid,bad]),'approve',acknowledge_market=True)
        self.assertEqual(self.execute('SELECT count(*) FROM price_alerts',fetch=True)[0][0],0)

    def test_same_stop_approval_does_not_rearm_existing_crossing(self):
        self.execute("INSERT INTO positions VALUES ('account','TEST','EQ',2,NOW())")
        self.execute("INSERT INTO key_levels(section,ticker,stop) VALUES ('positions','TEST',90)")
        existing=self.add_alert(source='key_levels_watch',source_key='position:TEST:stop',threshold=90,triggered=True)
        self.execute("UPDATE price_alerts SET condition='below' WHERE id=%s",(existing,))
        self.approve(self.stage(90,stop=True,evidence='TEST stop 90.'))
        self.assertTrue(self.alert(existing)['triggered'])
