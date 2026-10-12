import os
import unittest
from unittest.mock import patch

os.environ.setdefault('DATABASE_URL','postgresql://localhost/fake')
from morning_brief.journal_review import normalize_extraction


class JournalReviewTests(unittest.TestCase):
    def raw(self, evidence, **kw):
        return dict(ticker='SMH',condition='below',threshold=600,evidence=evidence,timeframe='price_crossing',**kw)

    def test_mention_is_context_not_alert(self):
        text='SMH leadership looks weak.'
        items=normalize_extraction({'commentary':[dict(ticker='SMH',evidence=text)]},text,'journal.md')
        self.assertEqual(items[0]['status'],'context')

    def test_alert_heading_is_watch_idea_not_notification(self):
        text='SMH Alerts: sustained break below $600.'
        items=normalize_extraction({'price_alerts':[self.raw(text)]},text,'journal.md')
        self.assertEqual(items[0]['status'],'idea')
        self.assertTrue(items[0]['needs_clarification'])
        self.assertEqual(items[0]['timeframe'],'sustained')

    def test_exact_explicit_request_pending_not_live(self):
        text='Alert me if SMH is below $600.00.'
        items=normalize_extraction({'price_alerts':[self.raw(text)]},text,'trading_journal_2026-10-05_to_2026-10-09.md')
        self.assertEqual(items[0]['status'],'pending')
        self.assertEqual(items[0]['source_date'].isoformat(),'2026-10-09')

    def test_watch_overlap_deduplicates_and_instruction_wins(self):
        text='Set an alert SMH above 640.'
        raw=dict(ticker='SMH',condition='above',threshold=640,evidence=text,timeframe='price_crossing')
        items=normalize_extraction({'price_alerts':[raw], 'watch_levels':[dict(raw,resistance=640,alert_above=640)]},text,'journal.md')
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['status'],'pending')

    def test_missing_or_fabricated_evidence_is_blocked(self):
        for evidence in ('','Alert me SMH below 600.'):
            items=normalize_extraction({'price_alerts':[self.raw(evidence)]},'SMH mentioned only.','journal.md')
            self.assertEqual(items[0]['status'],'blocked')

    def test_no_index_to_futures_substitution(self):
        text='Alert me if NDX drops below 31000.'
        item=dict(ticker='NQ=F',condition='below',threshold=31000,evidence=text,timeframe='price_crossing')
        self.assertEqual(normalize_extraction({'price_alerts':[item]},text,'journal.md')[0]['status'],'blocked')

    def test_decimal_and_number_boundaries(self):
        text='Alert me SMH below 6000.'
        self.assertEqual(normalize_extraction({'price_alerts':[self.raw(text)]},text,'journal.md')[0]['status'],'blocked')
        text='Alert me SMH below 54.950.'
        item=self.raw(text)
        item['threshold']=54.95
        self.assertEqual(normalize_extraction({'price_alerts':[item]},text,'journal.md')[0]['status'],'pending')

    def test_weekly_range_requires_clarification(self):
        text='SMH weekly close below 600–605.'
        item=normalize_extraction({'watch_levels':[dict(ticker='SMH',support=600,evidence=text,timeframe='price_crossing')]},text,'journal.md')[0]
        self.assertTrue(item['needs_clarification'])
        self.assertIn('weekly',item['timeframe'])

    def test_apply_only_calls_staging_not_compiler(self):
        from morning_brief.journal_sync import apply_extraction
        with patch('morning_brief.journal_sync.stage_extraction',return_value={'alerts':0}) as stage, \
             patch('morning_brief.alert_compiler.reconcile_structural_alerts') as structural, \
             patch('morning_brief.alert_compiler.reconcile_journal_alerts') as journal:
            self.assertEqual(apply_extraction({},text='sample',file_path='file.md'),{'alerts':0})
            stage.assert_called_once_with({},'sample','file.md')
            structural.assert_not_called()
            journal.assert_not_called()
