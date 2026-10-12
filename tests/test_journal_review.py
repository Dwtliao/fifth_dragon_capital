import os
import unittest
import io
import tempfile
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch

os.environ.setdefault('DATABASE_URL','postgresql://localhost/fake')
from morning_brief.journal_review import normalize_extraction


class JournalReviewTests(unittest.TestCase):
    def test_recent_journals_is_bounded_and_uses_modification_time(self):
        from morning_brief.journal_sync import recent_journals
        with tempfile.TemporaryDirectory() as directory:
            diary=Path(directory)
            files=[]
            for index in range(8):
                path=diary/f'trading_journal_week_{8-index}.md'
                path.touch()
                os.utime(path,(100+index,100+index))
                files.append(path)
            self.assertEqual(recent_journals(diary),files[-5:])
            self.assertEqual(recent_journals(diary,1),files[-1:])
            with self.assertRaises(ValueError): recent_journals(diary,6)

    def test_default_cli_checks_only_five_and_does_not_force(self):
        from morning_brief import journal_sync
        files=[Path(f'journal_{i}.md') for i in range(5)]
        with patch('sys.argv',['journal_sync']), \
             patch('morning_brief.journal_sync.recent_journals',return_value=files) as recent, \
             patch('morning_brief.journal_sync.process_file',return_value={}) as process, \
             redirect_stderr(io.StringIO()), patch('sys.stdout',new_callable=io.StringIO):
            journal_sync.main()
        self.assertEqual(process.call_count,5)
        self.assertEqual(recent.call_args.args[1],5)
        self.assertTrue(all(not call.kwargs['force'] for call in process.call_args_list))

    def raw(self, evidence, **kw):
        return dict(ticker='SMH',condition='below',threshold=600,evidence=evidence,timeframe='price_crossing',**kw)

    def test_mention_is_context_not_alert(self):
        text='SMH leadership looks weak.'
        items=normalize_extraction({'commentary':[dict(ticker='SMH',evidence=text)]},text,'journal.md')
        self.assertEqual(items[0]['status'],'context')

    def test_string_commentary_does_not_discard_valid_actionable_ideas(self):
        text='Alert me if SMH is below 600.'
        actionable={'price_alerts':[self.raw(text)]}
        expected=normalize_extraction(actionable,text,'journal.md')
        with redirect_stderr(io.StringIO()) as warnings:
            actual=normalize_extraction(dict(actionable,commentary=['SMH leadership looks weak.']),text,'journal.md')
        self.assertEqual(actual,expected)
        self.assertIn('ignored 1 malformed optional commentary',warnings.getvalue())

    def test_mixed_commentary_preserves_objects_without_inventing_context(self):
        text='SMH leadership looks weak.'
        with redirect_stderr(io.StringIO()):
            items=normalize_extraction({'commentary':['Plain text',None,dict(ticker='SMH',evidence=text)]},text,'journal.md')
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['status'],'context')

    def test_malformed_optional_commentary_container_is_safe(self):
        for value in ('Plain text',{'ticker':'SMH'},123,None):
            with self.subTest(value=value), redirect_stderr(io.StringIO()):
                self.assertEqual(normalize_extraction({'commentary':value},'sample','journal.md'),[])

    def test_malformed_actionable_entries_fail_before_database_access(self):
        from morning_brief.journal_review import stage_extraction
        for field in ('positions','watch_levels','price_alerts'):
            with self.subTest(field=field), patch('morning_brief.journal_review.get_connection') as connect:
                with self.assertRaisesRegex(ValueError,field+' must be an array of objects'):
                    stage_extraction({field:['not an object']},'sample','journal.md')
                connect.assert_not_called()

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
