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
            self.assertIsNotNone(save.call_args.args[4].tzinfo)

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
