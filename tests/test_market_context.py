import datetime
import unittest
from unittest.mock import patch

import pandas as pd

from morning_brief import formatter
from morning_brief.market_context import (
    MARKET_GROUPS, build_cross_market_signals, fetch_market_overview, summarize_history,
)


class MarketContextTests(unittest.TestCase):
    def setUp(self):
        self.dates = pd.bdate_range("2026-07-20", periods=60)
        self.today = self.dates[-1].date()
        self.history = pd.DataFrame({"Close": range(100, 160)}, index=self.dates, dtype=float)

    def test_returns_use_trading_bars_and_full_window(self):
        row = summarize_history("Example", "SPY", self.history, self.today)
        self.assertAlmostEqual(row["change_1d"], (159 / 158 - 1) * 100)
        self.assertAlmostEqual(row["change_5d"], (159 / 154 - 1) * 100)
        self.assertAlmostEqual(row["change_20d"], (159 / 139 - 1) * 100)
        self.assertEqual(row["ma20"], 149.5)
        self.assertEqual(row["ma50"], 134.5)
        self.assertEqual((row["low_20d"], row["high_20d"]), (140, 159))
        self.assertTrue(row["above_ma20"])
        self.assertEqual(row["as_of"], self.today.isoformat())

    def test_yield_changes_are_basis_points_without_rescaling_level(self):
        history = pd.DataFrame({"Close": [4.10, 4.25]}, index=self.dates[-2:])
        row = summarize_history("10Y yield", "^TNX", history, self.today)
        self.assertEqual(row["last"], 4.25)
        self.assertEqual(row["unit"], "yield_pct")
        self.assertAlmostEqual(row["change_1d"], 15)
        self.assertIsNone(row["change_5d"])
        self.assertIsNone(row["ma20"])

    def test_nan_gaps_do_not_turn_sessions_into_calendar_days(self):
        history = self.history.copy()
        history.loc[self.dates[-2], "Close"] = float("nan")
        row = summarize_history("Example", "SPY", history, self.today)
        self.assertAlmostEqual(row["change_1d"], (159 / 157 - 1) * 100)

    def test_stale_and_short_history_are_explicit(self):
        row = summarize_history("Example", "SPY", self.history, self.today + datetime.timedelta(days=5))
        self.assertTrue(row["stale"])
        short = summarize_history("Example", "SPY", self.history.tail(1), self.today)
        self.assertIn("error", short)

    def test_infinite_values_are_excluded(self):
        history = self.history.copy()
        history.loc[self.dates[-1], "Close"] = float("inf")
        row = summarize_history("Example", "SPY", history, self.today)
        self.assertEqual(row["last"], 158)

    def test_ratios_align_dates_instead_of_using_each_latest_quote(self):
        histories = {
            "RSP": pd.DataFrame({"Close": [100, 110, 900]}, index=self.dates[-3:]),
            "SPY": pd.DataFrame({"Close": [200, 200]}, index=self.dates[-3:-1]),
        }
        signal = next(s for s in build_cross_market_signals(histories, self.today)
                      if s["label"] == "Equal weight / S&P 500")
        self.assertEqual(signal["last"], 0.55)
        self.assertEqual(signal["as_of"], self.dates[-2].date().isoformat())
        self.assertAlmostEqual(signal["change_1d"], 10)

    def test_yield_gap_change_is_absolute_basis_points(self):
        histories = {
            "^TYX": pd.DataFrame({"Close": [4.5, 4.6]}, index=self.dates[-2:]),
            "^TNX": pd.DataFrame({"Close": [4.2, 4.2]}, index=self.dates[-2:]),
        }
        signal = build_cross_market_signals(histories, self.today)[0]
        self.assertAlmostEqual(signal["last"], 40)
        self.assertAlmostEqual(signal["change_1d"], 10)
        self.assertEqual(signal["unit"], "bp")

    def test_zero_denominators_and_no_shared_dates_are_safe(self):
        histories = {
            "RSP": pd.DataFrame({"Close": [100]}, index=self.dates[-1:]),
            "SPY": pd.DataFrame({"Close": [0]}, index=self.dates[-1:]),
            "GC=F": pd.DataFrame({"Close": [3000]}, index=self.dates[-1:]),
            "SI=F": pd.DataFrame({"Close": [30]}, index=self.dates[:1]),
        }
        signals = build_cross_market_signals(histories, self.today)
        self.assertTrue(all("error" in row for row in signals))

    def test_partial_download_keeps_available_symbols_and_batches_once(self):
        data = pd.concat({"^TNX": self.history / 40, "CL=F": self.history}, axis=1)
        with patch("morning_brief.market_context.yf.download", return_value=data) as download:
            overview = fetch_market_overview()
        download.assert_called_once()
        self.assertEqual(download.call_args.kwargs["period"], "6mo")
        self.assertEqual(download.call_args.kwargs["group_by"], "ticker")
        energy = overview["groups"]["energy"]
        self.assertNotIn("error", energy[0])
        self.assertIn("error", energy[1])
        self.assertEqual(set(overview["groups"]), set(MARKET_GROUPS))

    def test_download_failure_does_not_raise_or_invent_values(self):
        with patch("morning_brief.market_context.yf.download", side_effect=RuntimeError("unavailable")):
            overview = fetch_market_overview()
        self.assertTrue(all("error" in row for rows in overview["groups"].values() for row in rows))
        self.assertIn("unavailable", overview["groups"]["rates"][0]["error"])

    def test_formatter_shows_units_dates_missing_data_and_proxy_limits(self):
        row = summarize_history("10Y yield", "^TNX",
                                pd.DataFrame({"Close": [4.10, 4.25]}, index=self.dates[-2:]), self.today)
        overview = {"fetched_at": "2026-10-08T12:00:00-04:00", "groups": {"rates": [row]}, "signals": []}
        output = formatter.render_market_overview(overview)
        self.assertIn("4.250%", output)
        self.assertIn("+15.00 bp", output)
        self.assertIn(self.today.isoformat(), output)
        self.assertIn("not a measured credit spread", output)
        self.assertIn("not spot uranium", output)

    def test_session_colors_and_percentage_boundaries(self):
        for value, color in ((0.01, 'green'), (0, 'orange'), (-0.01, 'orange'),
                             (-2.5, 'orange'), (-2.51, 'red')):
            self.assertTrue(formatter._market_change(value).startswith(f':{color}['))
        self.assertEqual(formatter._market_change(None), '—')
        self.assertEqual(formatter._market_change(-1, 'bp'), ':red[-1.00 bp]')
        self.assertEqual(formatter._market_change(1, 'yield_pct'), ':green[+1.00 bp]')

    def test_moving_average_colors_are_independent_and_missing_is_neutral(self):
        row = {'label': 'Test', 'unit': 'price', 'last': 100, 'as_of': self.today.isoformat(),
               'above_ma20': True, 'above_ma50': False}
        overview = {'fetched_at': '2026-10-08T12:00:00-04:00',
                    'groups': {'global': [row]}, 'signals': []}
        self.assertIn(':green[above] / :red[at/below]', formatter.render_market_overview(overview))
        row['above_ma20'] = None
        self.assertIn('— / :red[at/below]', formatter.render_market_overview(overview))

    def test_market_failure_does_not_prevent_positions_or_watch_sections(self):
        from morning_brief import brief

        with patch("morning_brief.brief.fetch_market_overview", side_effect=ValueError("market offline")), \
             patch("morning_brief.fetchers.load_key_levels_from_db", return_value={}), \
             patch("morning_brief.fetchers.fetch_fed_events", return_value=[]), \
             patch("morning_brief.fetchers.fetch_positions", return_value=[{"label": "Holding"}]), \
             patch("morning_brief.fetchers.fetch_watch_levels", return_value=[{"label": "Watch"}]), \
             patch("morning_brief.formatter.render_positions", return_value="holdings retained"), \
             patch("morning_brief.formatter.render_key_levels", return_value="watch retained"):
            output = brief.generate_brief()
        self.assertIn("market offline", output)
        self.assertIn("holdings retained", output)
        self.assertIn("watch retained", output)
