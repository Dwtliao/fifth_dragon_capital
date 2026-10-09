import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from morning_brief.analysis import build_evidence, render_analysis
from morning_brief import brief


class BriefAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.portfolio_flag = patch.dict("os.environ", {"CLAUDE_BRIEF_INCLUDE_PORTFOLIO": "true"})
        self.portfolio_flag.start()
        self.addCleanup(self.portfolio_flag.stop)
        self.overview = {"fetched_at": "2026-10-08T12:00:00-04:00", "groups": {
            "rates": [{"label": "10Y yield", "ticker": "^TNX", "last": 4.25,
                       "unit": "yield_pct", "as_of": "2026-10-08", "change_1d": 15}],
        }, "signals": []}
        self.positions = [{"ticker": "URNM", "quantity": 10, "last": 55, "stop": 54.95},
                          {"ticker": "OLD", "quantity": None, "last": 100}]
        self.analysis = {
            "developments": [{"text": "Yield rose 15 basis points.", "evidence_ids": ["market:^TNX"]}],
            "portfolio": [{"ticker": "URNM", "text": "Review proximity to the saved stop.",
                           "evidence_ids": ["holding:URNM"]}],
            "conditions": [],
        }

    def response(self, analysis=None):
        return {"text": json.dumps(analysis or self.analysis), "model": "claude-sonnet-5-5",
                "effort": "medium", "input_tokens": 1000, "output_tokens": 200}

    def test_only_current_holdings_and_calculated_stop_distance_are_supplied(self):
        payload = build_evidence(self.overview, self.positions, {"watch": {"GC=F": {"support": 4300}}})
        self.assertEqual(payload["current_holdings"], ["URNM"])
        self.assertNotIn("holding:OLD", payload["evidence"])
        self.assertIn("watch:GC=F", payload["evidence"])
        self.assertAlmostEqual(payload["evidence"]["holding:URNM"]["distance_above_saved_stop_pct"],
                               (55 / 54.95 - 1) * 100)

    def test_render_has_traceable_evidence_and_model_usage(self):
        with patch("morning_brief.analysis.call_claude", return_value=self.response()) as call:
            output = render_analysis(self.overview, self.positions, {})
        self.assertIn("What matters this morning", output)
        self.assertIn("10Y yield (2026-10-08)", output)
        self.assertIn("claude-sonnet-5-5", output)
        self.assertEqual(call.call_args.args[1], "brief")

    def test_unknown_evidence_and_nonholding_tickers_are_rejected(self):
        invalid = {**self.analysis, "developments": [{"text": "invented", "evidence_ids": ["market:UNKNOWN"]}]}
        with patch("morning_brief.analysis.call_claude", return_value=self.response(invalid)):
            with self.assertRaisesRegex(ValueError, "evidence"):
                render_analysis(self.overview, self.positions, {})
        invalid = {**self.analysis, "portfolio": [{"ticker": "OLD", "text": "invented holding",
                                                   "evidence_ids": ["market:^TNX"]}]}
        with patch("morning_brief.analysis.call_claude", return_value=self.response(invalid)):
            with self.assertRaisesRegex(ValueError, "absent"):
                render_analysis(self.overview, self.positions, {})

    def test_disabled_and_missing_market_data_do_not_call_api(self):
        with patch("morning_brief.analysis.call_claude") as call, \
             patch.dict("os.environ", {"CLAUDE_BRIEF_ANALYSIS": "false"}):
            self.assertEqual(render_analysis(self.overview, [], {}), "")
            call.assert_not_called()

    def test_market_only_mode_does_not_send_private_holdings_or_levels(self):
        analysis = {**self.analysis, "portfolio": []}
        with patch.dict("os.environ", {"CLAUDE_BRIEF_INCLUDE_PORTFOLIO": "false"}), \
             patch("morning_brief.analysis.call_claude", return_value=self.response(analysis)) as call:
            output = render_analysis(self.overview, self.positions, {"watch": {"PRIVATE": {"support": 12}}})
        payload = json.loads(call.call_args.args[0].split("Evidence:\n", 1)[1])
        self.assertEqual(payload["current_holdings"], [])
        self.assertNotIn("holding:URNM", payload["evidence"])
        self.assertNotIn("watch:PRIVATE", payload["evidence"])
        self.assertIn("Market-only analysis", output)
        with patch("morning_brief.analysis.call_claude") as call:
            output = render_analysis({**self.overview, "groups": {}}, [], {})
            self.assertIn("no usable market data", output)
            call.assert_not_called()

    def generate(self, analysis_error=None):
        with ExitStack() as stack:
            for target, result in (
                ("morning_brief.fetchers.load_key_levels_from_db", {}),
                ("morning_brief.brief.attention_summary", "ATTENTION SUMMARY"),
                ("morning_brief.fetchers.fetch_fed_events", []),
                ("morning_brief.brief.fetch_market_overview", self.overview),
                ("morning_brief.fetchers.fetch_positions", self.positions),
                ("morning_brief.fetchers.fetch_watch_levels", []),
                ("morning_brief.formatter.render_market_overview", "MARKET TABLES"),
                ("morning_brief.formatter.render_positions", "HOLDINGS TABLE"),
            ):
                stack.enter_context(patch(target, return_value=result))
            render = stack.enter_context(patch("morning_brief.brief.render_analysis",
                                               return_value="WHAT MATTERS", side_effect=analysis_error))
            output = brief.generate_brief()
            render.assert_called_once_with(self.overview, self.positions, {})
            return output

    def test_real_brief_path_calls_analysis_without_a_journal(self):
        output = self.generate()
        self.assertLess(output.index("WHAT MATTERS"), output.index("MARKET TABLES"))
        self.assertLess(output.index("ATTENTION SUMMARY"), output.index("WHAT MATTERS"))

    def test_api_failure_leaves_a_visible_message_and_market_tables(self):
        output = self.generate(RuntimeError("API unavailable"))
        self.assertIn("Claude analysis unavailable", output)
        self.assertIn("MARKET TABLES", output)
        self.assertIn("HOLDINGS TABLE", output)
