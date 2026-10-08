import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.morning_pipeline import run_command, run_morning_pipeline


class MorningPipelineTests(unittest.TestCase):
    def test_sync_refreshes_holdings_before_brief(self):
        holdings = ["old"]
        calls = []

        def runner(name, command, root):
            calls.append(name)
            if name == "E*TRADE sync":
                holdings[:] = ["new"]
            if name == "Brief":
                self.assertEqual(holdings, ["new"])
            return {"name": name, "status": "success", "returncode": 0, "output": name}

        with tempfile.TemporaryDirectory() as directory:
            diary = Path(directory)
            (diary / "trading_journal_today.md").touch()
            steps = run_morning_pipeline(diary, diary, lambda: True, runner)
        self.assertEqual(calls, ["Journal", "E*TRADE sync", "Brief"])
        self.assertTrue(all(s["status"] == "success" for s in steps))

    def test_stale_token_skips_sync_but_records_reason_and_generates_brief(self):
        calls = []

        def runner(name, command, root):
            calls.append(name)
            return {"name": name, "status": "success", "returncode": 0, "output": ""}

        with tempfile.TemporaryDirectory() as directory:
            steps = run_morning_pipeline(Path(directory), Path(directory), lambda: False, runner)
        self.assertEqual(calls, ["Brief"])
        self.assertEqual(steps[0]["status"], "skipped")
        self.assertEqual(steps[1]["status"], "skipped")
        self.assertIn("Re-authenticate", steps[1]["output"])

    def test_partial_sync_failure_does_not_hide_brief_result(self):
        def runner(name, command, root):
            return {"name": name, "status": "failed" if name == "E*TRADE sync" else "success",
                    "returncode": 1 if name == "E*TRADE sync" else 0,
                    "output": "orders: account1: timeout" if name == "E*TRADE sync" else "brief saved"}

        with tempfile.TemporaryDirectory() as directory:
            steps = run_morning_pipeline(Path(directory), Path(directory), lambda: True, runner)
        self.assertEqual(steps[1]["status"], "failed")
        self.assertIn("timeout", steps[1]["output"])
        self.assertEqual(steps[2]["status"], "success")

    def test_command_retains_both_streams_and_marks_source(self):
        with patch("dashboard.morning_pipeline.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 1, "partial holdings\n", "orders failed\n")) as run:
            result = run_command("E*TRADE sync", ["python", "-m", "etrade_sync", "sync"], Path("."))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["output"], "partial holdings\norders failed")
        self.assertEqual(run.call_args.kwargs["env"]["SYNC_TRIGGERED_BY"], "p10_morning_pipeline")

    def test_launch_error_is_a_visible_failure(self):
        with patch("dashboard.morning_pipeline.subprocess.run", side_effect=OSError("Cannot launch")):
            result = run_command("Brief", ["missing"], Path("."))
        self.assertEqual(result["status"], "failed")
        self.assertIn("Cannot launch", result["output"])


class MorningPipelineUITests(unittest.TestCase):
    def test_results_and_stale_warning_survive_rerun(self):
        from streamlit.testing.v1 import AppTest

        os.environ.setdefault("DATABASE_URL", "postgresql://localhost/fake")
        page = Path(__file__).resolve().parents[1] / "dashboard/pages/P10_Morning_Brief.py"
        steps = [
            {"name": "Journal", "status": "success", "returncode": 0, "output": "journal saved"},
            {"name": "E*TRADE sync", "status": "failed", "returncode": 1, "output": "orders: account1: timeout"},
            {"name": "Brief", "status": "success", "returncode": 0, "output": "brief saved"},
        ]
        with patch("morning_brief.fetchers.load_key_levels_from_db", return_value={}), \
             patch("morning_brief.fetchers.fetch_positions_from_db", return_value=[]), \
             patch("dashboard.db.query", return_value=[]), \
             patch("dashboard.morning_pipeline.run_morning_pipeline", return_value=steps) as pipeline:
            app = AppTest.from_file(str(page)).run()
            self.assertEqual(len(app.exception), 0)
            next(b for b in app.sidebar.button if "Run Morning Pipeline" in b.label).click().run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.session_state["p10_pipeline_result"]["steps"], steps)
            self.assertTrue(any("E*TRADE sync: failed" in e.value for e in app.sidebar.error))
            self.assertTrue(any("stale or incomplete" in w.value for w in app.warning))
            self.assertTrue(any("orders: account1: timeout" in c.value for c in app.sidebar.code))
            app.run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any("E*TRADE sync: failed" in e.value for e in app.sidebar.error))
            self.assertTrue(any("stale or incomplete" in w.value for w in app.warning))
            pipeline.assert_called_once()
