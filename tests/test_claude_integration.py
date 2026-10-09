import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/fake")
os.environ.setdefault("ETRADE_CONSUMER_KEY", "test-key")
os.environ.setdefault("ETRADE_CONSUMER_SECRET", "test-secret")

from morning_brief import journal_sync, llm


class ClaudeIntegrationTests(unittest.TestCase):
    def response(self, text="answer", stop_reason="end_turn"):
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="private reasoning"),
                     SimpleNamespace(type="text", text=text)],
            stop_reason=stop_reason, model="claude-sonnet-5-5",
            usage=SimpleNamespace(input_tokens=500, output_tokens=100),
        )

    def test_default_model_and_task_efforts_handle_thinking_blocks(self):
        with patch("morning_brief.llm.anthropic.Anthropic") as client, \
             patch("morning_brief.llm.os.getenv", side_effect=lambda key, default: default):
            create = client.return_value.messages.create
            create.return_value = self.response()
            journal = llm.call_claude("extract", "journal")
            self.assertEqual(create.call_args.kwargs["model"], "claude-sonnet-5-5")
            self.assertEqual(create.call_args.kwargs["output_config"], {"effort": "low"})
            self.assertEqual(journal["text"], "answer")
            self.assertEqual(journal["input_tokens"], 500)
            llm.call_claude("interpret", "brief")
            self.assertEqual(create.call_args.kwargs["output_config"], {"effort": "medium"})

    def test_environment_and_explicit_overrides(self):
        with patch("morning_brief.llm.anthropic.Anthropic") as client, \
             patch.dict(os.environ, {"CLAUDE_MODEL": "claude-sonnet-5", "CLAUDE_JOURNAL_EFFORT": "medium"}):
            create = client.return_value.messages.create
            create.return_value = self.response()
            llm.call_claude("extract", "journal")
            self.assertEqual(create.call_args.kwargs["model"], "claude-sonnet-5")
            self.assertEqual(create.call_args.kwargs["output_config"], {"effort": "medium"})
            llm.call_claude("extract", "journal", model="claude-sonnet-4-6", effort="low")
            self.assertEqual(create.call_args.kwargs["model"], "claude-sonnet-4-6")
            self.assertEqual(create.call_args.kwargs["output_config"], {"effort": "low"})

    def test_invalid_effort_does_not_call_api(self):
        with patch("morning_brief.llm.anthropic.Anthropic") as client:
            with self.assertRaisesRegex(ValueError, "effort"):
                llm.call_claude("extract", "journal", effort="light")
            client.assert_not_called()

    def test_truncated_and_empty_responses_are_rejected(self):
        with patch("morning_brief.llm.anthropic.Anthropic") as client:
            client.return_value.messages.create.return_value = self.response(stop_reason="max_tokens")
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                llm.call_claude("extract", "journal")
            response = self.response()
            response.content = [SimpleNamespace(type="thinking", thinking="no final answer")]
            client.return_value.messages.create.return_value = response
            with self.assertRaisesRegex(RuntimeError, "no answer text"):
                llm.call_claude("extract", "journal")

    def test_json_extraction_and_invalid_schema(self):
        extraction = {"positions": [], "watch_levels": [], "price_alerts": []}
        with patch("morning_brief.journal_sync.call_claude") as call, redirect_stdout(io.StringIO()):
            result = {"text": "```json\n" + json.dumps(extraction) + "\n```",
                      "model": "claude-sonnet-5-5", "effort": "low", "input_tokens": 500, "output_tokens": 100}
            call.return_value = result
            self.assertEqual(journal_sync.extract_from_journal("sample"), extraction)
            call.return_value = {**result, "text": '{"positions": "incorrect"}'}
            with self.assertRaisesRegex(ValueError, "arrays"):
                journal_sync.extract_from_journal("sample")

    def test_dry_run_has_no_database_dependency_or_writes(self):
        extraction = {"positions": [{"ticker": "URNM", "stop": 54.95}],
                      "watch_levels": [], "price_alerts": []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.md"
            path.touch()
            with patch("morning_brief.journal_sync.extract_from_journal", return_value=extraction) as extract, \
                 patch("morning_brief.journal_sync._already_synced") as already, \
                 patch("morning_brief.journal_sync.apply_extraction") as apply, \
                 patch("morning_brief.journal_sync._log_sync") as log, \
                 patch("morning_brief.journal_sync.get_connection", side_effect=AssertionError("DB accessed")), \
                 redirect_stdout(io.StringIO()):
                counts = journal_sync.process_file(path, dry_run=True, model="claude-sonnet-5", effort="medium")
                self.assertEqual(counts, {"positions": 1, "watch": 0, "alerts": 0})
                extract.assert_called_once_with("", model="claude-sonnet-5", effort="medium")
                already.assert_not_called()
                apply.assert_not_called()
                log.assert_not_called()
