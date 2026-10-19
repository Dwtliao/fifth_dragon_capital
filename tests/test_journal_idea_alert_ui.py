"""Manual idea form tests: no production database, quotes or delivery."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('DATABASE_URL', 'postgresql://localhost/fake')
from streamlit.testing.v1 import AppTest


class IdeaAlertFormTests(unittest.TestCase):
    def app(self):
        return AppTest.from_string('''
from dashboard.journal_review_ui import render_idea_alert
render_idea_alert(dict(id=58,ticker='SI=F',condition='below',threshold=47), 'test')
''').run()

    def test_edit_preview_and_create_uses_explicit_manual_rule(self):
        with patch('dashboard.journal_review_ui.save_manual', return_value=123) as save:
            app = self.app()
            self.assertFalse(app.exception)
            self.assertEqual(app.text_input(key='test_symbol').value, 'SI=F')
            save.assert_not_called()
            app.text_input(key='test_symbol').set_value('SLV').run()
            app.selectbox(key='test_direction').select('above').run()
            app.number_input(key='test_price').set_value(50.0).run()
            app.button(key='test_preview_manual').click().run()
            save.assert_not_called()
            app.button(key='test_create_manual').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(save.call_args.args[:4],
                ('SLV', 'Manual alert from journal idea #58', 'above', 50.0))
            self.assertEqual(len(save.call_args.args), 4)
            self.assertFalse(any('Expir' in n.label for n in app.number_input))

    def test_edit_invalidates_preview_and_invalid_input_cannot_create(self):
        with patch('dashboard.journal_review_ui.save_manual') as save:
            app = self.app()
            app.button(key='test_preview_manual').click().run()
            self.assertTrue(any(b.key=='test_create_manual' for b in app.button))
            app.number_input(key='test_price').set_value(48.0).run()
            self.assertFalse(any(b.key=='test_create_manual' for b in app.button))
            app.text_input(key='test_symbol').set_value('').run()
            app.button(key='test_preview_manual').click().run()
            self.assertTrue(app.error)
            save.assert_not_called()

    def test_duplicate_or_save_error_stays_visible(self):
        with patch('dashboard.journal_review_ui.save_manual',
                   side_effect=ValueError('Exact existing level in alert IDs [7]')):
            app = self.app()
            app.button(key='test_preview_manual').click().run()
            app.button(key='test_create_manual').click().run()
            self.assertIn('Exact existing level', app.error[0].value)
            self.assertFalse(app.exception)


class WatchIdeaRejectionTests(unittest.TestCase):
    def test_watch_selection_can_only_reject_even_with_stale_approve_state(self):
        from morning_brief.journal_review import item_token
        for status, view in (('idea', 'Watch ideas'), ('pending', 'Pending approval')):
            item = dict(id=58, revision=1, ticker='TEST', kind='watch', status=status,
                        condition='below', threshold=47, timeframe='price', evidence='Test evidence',
                        validation_error=None, needs_clarification=False, source_date=None,
                        file_path='/fake/journal.md')
            prefix = f"jr_58_{item_token(item)[:12]}"
            with self.subTest(status=status), \
                 patch('dashboard.journal_review_ui.list_items', return_value=[item]), \
                 patch('dashboard.journal_review_ui.current_alerts', return_value=[]), \
                 patch('dashboard.journal_review_ui.preview_selection', return_value=[{'id':58}]) as preview, \
                 patch('dashboard.journal_review_ui.review_action', return_value=[58]) as review, \
                 patch('dashboard.journal_review_ui.save_manual') as save:
                app = AppTest.from_string('''
import streamlit as st
from dashboard.journal_review_ui import render_journal_review
render_journal_review()
''')
                app.session_state['jr_action'] = 'approve'
                app.run()
                app.radio(key='jr_view').set_value(view).run()
                app.checkbox(key=prefix+'_selected').check().run()
                self.assertFalse(any(s.key=='jr_action' for s in app.selectbox))
                app.button(key='jr_preview_button').click().run()
                preview.assert_called_once()
                confirm = next(c for c in app.checkbox if c.key.startswith('jr_confirm_'))
                confirm.check().run()
                app.button(key='jr_apply').click().run()
                self.assertFalse(app.exception)
                self.assertEqual(review.call_args.args, ([{'id':58}], 'reject'))
                save.assert_not_called()
