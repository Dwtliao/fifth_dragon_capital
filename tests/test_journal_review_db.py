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

    def test_purged_watch_alert_cannot_be_silently_restored(self):
        from alerts.management import apply_bulk,preview_action
        old=self.add_alert(threshold=110)
        self.execute("UPDATE price_alerts SET condition='below' WHERE id=%s",(old,))
        apply_bulk(preview_action([self.alert(old)],'purge'),'purge')
        with self.assertRaises(ValueError): self.approve(self.stage(110))
        self.assertEqual(self.execute('SELECT count(*) FROM price_alerts',fetch=True)[0][0],0)

    def test_compiler_reconcile_blocks_on_concurrent_stop_approval_lock(self):
        """The compiler's structural reconcile must serialize per-ticker with an
        in-flight stop approval, or it can overwrite the approval with a stale
        read (lost update). Demonstrated by holding the same per-ticker lock an
        approval would hold, open on a separate connection, and confirming the
        compiler blocks until it is released rather than proceeding immediately."""
        import hashlib
        import threading
        from morning_brief import alert_compiler
        self.execute("INSERT INTO key_levels(section,ticker,stop) VALUES ('positions','TEST',90)")
        self.add_alert(source='key_levels_watch',source_key='position:TEST:stop',threshold=90)
        holder=self.connect()
        self.addCleanup(holder.close)  # Guarantee the held lock is released even if an assertion fails.
        symbol=int.from_bytes(hashlib.sha256(b'TEST').digest()[:4],'big',signed=True)
        with holder.cursor() as cur:
            cur.execute('SELECT pg_advisory_lock(%s,%s)',(lifecycle.LOCK_NAMESPACE+1,symbol))
        result={}
        def run():
            with patch('morning_brief.alert_compiler.get_connection',self.connect):
                result['stats']=alert_compiler.reconcile_structural_alerts({'positions':{'TEST':{'stop':95}}})
        t=threading.Thread(target=run,daemon=True)
        t.start()
        t.join(timeout=0.5)
        self.assertTrue(t.is_alive(),'reconcile should block while a concurrent approval holds the ticker lock')
        self.assertEqual(self.alert(self.execute("SELECT id FROM price_alerts",fetch=True)[0][0])['threshold'],90)
        holder.rollback(); holder.close()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(),'reconcile should proceed once the lock is released')
        self.assertEqual(result['stats']['updated'],1)
        self.assertEqual(self.alert(self.execute("SELECT id FROM price_alerts",fetch=True)[0][0])['threshold'],95)

    def test_compiler_reload_prevents_stale_stop_overwrite(self):
        """Reproduces the $90->$95 scenario: a compiler run that read key_levels
        before an approval committed must not, once it finally gets the lock,
        write the stop back down to the value it read before. It must reload
        the authoritative row under the lock instead of trusting its stale
        pre-lock snapshot."""
        from morning_brief import alert_compiler
        self.execute("INSERT INTO positions VALUES ('account','TEST','EQ',2,NOW())")
        self.execute("INSERT INTO key_levels(section,ticker,stop) VALUES ('positions','TEST',90)")
        existing=self.add_alert(source='key_levels_watch',source_key='position:TEST:stop',threshold=90)
        self.execute("UPDATE price_alerts SET condition='below',tier=1,pinned=FALSE,label='Stop: TEST below 90' WHERE id=%s",(existing,))
        stale_key_levels={'positions':{'TEST':{'stop':90}}}  # read before the approval below commits
        self.approve(self.stage(95,stop=True,evidence='TEST stop 95.'))
        self.assertEqual(self.alert(existing)['threshold'],95)
        def reload():
            row=self.execute("SELECT stop FROM key_levels WHERE section='positions' AND ticker='TEST'",fetch=True)[0]
            return {'positions':{'TEST':{'stop':float(row[0])}}}
        with patch('morning_brief.alert_compiler.get_connection',self.connect):
            stats=alert_compiler.reconcile_structural_alerts(stale_key_levels,reload_key_levels=reload)
        self.assertEqual(stats['updated'],0)
        self.assertEqual(stats['unchanged'],1)
        self.assertEqual(self.execute("SELECT stop FROM key_levels WHERE section='positions' AND ticker='TEST'",fetch=True)[0][0],95)
        self.assertEqual(self.alert(existing)['threshold'],95)

    def test_compiler_acquires_same_guard_lock_as_stop_approval_in_matching_order(self):
        """The compiler must take the identical (key, order) locks a stop
        approval does: guard before per-ticker symbol. If it took them in the
        opposite order (or a differently-keyed lock), the two could deadlock
        under real concurrency instead of one simply queuing behind the other."""
        import threading
        from morning_brief import alert_compiler
        self.execute("INSERT INTO key_levels(section,ticker,stop) VALUES ('positions','TEST',90)")
        self.add_alert(source='key_levels_watch',source_key='position:TEST:stop',threshold=90)
        holder=self.connect()
        self.addCleanup(holder.close)
        with holder.cursor() as cur:
            # Exactly the guard key/order review_action's stop-mode approval takes first.
            cur.execute('SELECT pg_advisory_lock(%s,hashtext(%s))',
                        (lifecycle.LOCK_NAMESPACE+2,'key_levels_watch:TEST:below'))
        result={}
        def run():
            with patch('morning_brief.alert_compiler.get_connection',self.connect):
                result['stats']=alert_compiler.reconcile_structural_alerts({'positions':{'TEST':{'stop':95}}})
        t=threading.Thread(target=run,daemon=True)
        t.start()
        t.join(timeout=0.5)
        self.assertTrue(t.is_alive(),'reconcile should queue behind the same guard key a stop approval holds')
        holder.rollback(); holder.close()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result['stats']['updated'],1)

    def test_p10_save_rejects_stale_baseline_instead_of_clobbering_approval(self):
        """Reproduces the P10 scenario: a page loaded stop=90, an approval moved
        it to 95, and the (stale) P10 widgets are then saved. Must refuse, not
        silently write 90 back over the approved 95."""
        from morning_brief import alert_compiler
        self.execute("INSERT INTO positions VALUES ('account','TEST','EQ',2,NOW())")
        baseline={'positions':{'TEST':{'stop':90}}}
        self.execute("INSERT INTO key_levels(section,ticker,stop) VALUES ('positions','TEST',90)")
        existing=self.add_alert(source='key_levels_watch',source_key='position:TEST:stop',threshold=90)
        self.execute("UPDATE price_alerts SET condition='below',tier=1,pinned=FALSE,label='Stop: TEST below 90' WHERE id=%s",(existing,))
        self.approve(self.stage(95,stop=True,evidence='TEST stop 95.'))  # concurrent approval, after baseline was loaded
        stale_edit={'positions':{'TEST':{'stop':90}}}  # P10 widget still showing the old value
        with patch('morning_brief.alert_compiler.get_connection',self.connect):
            with self.assertRaises(ValueError):
                alert_compiler.save_structural_key_levels(stale_edit,baseline)
        self.assertEqual(self.execute("SELECT stop FROM key_levels WHERE section='positions' AND ticker='TEST'",fetch=True)[0][0],95)
        self.assertEqual(self.alert(existing)['threshold'],95)

    def test_p10_save_succeeds_when_baseline_matches_current(self):
        from morning_brief import alert_compiler
        baseline={'positions':{'TEST':{'stop':90}}}
        self.execute("INSERT INTO key_levels(section,ticker,stop) VALUES ('positions','TEST',90)")
        edit={'positions':{'TEST':{'stop':92}}}
        with patch('morning_brief.alert_compiler.get_connection',self.connect):
            stats=alert_compiler.save_structural_key_levels(edit,baseline)
        self.assertNotIn('error',stats)
        self.assertEqual(self.execute("SELECT stop FROM key_levels WHERE section='positions' AND ticker='TEST'",fetch=True)[0][0],92)

    def test_resync_after_purge_does_not_reenter_queue(self):
        from alerts.management import apply_bulk,preview_action
        item=self.stage(110)
        self.approve(item)
        accepted=next(i for i in review.list_items() if i['id']==item['id'])
        apply_bulk(preview_action([self.alert(accepted['alert_id'])],'purge'),'purge')
        repeated=self.stage(110)
        self.assertEqual(repeated['id'],item['id'])
        self.assertEqual(repeated['status'],'accepted')

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
