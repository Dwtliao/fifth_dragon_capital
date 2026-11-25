import datetime as dt
import unittest
from datetime import date

from morning_brief import event_dates as ed

D = date

# Read from the Cboe 2026 Options Expiration Calendar (WF-242411).
CBOE_2026_HOLIDAYS = {D(2026, 1, 1), D(2026, 1, 19), D(2026, 2, 16), D(2026, 4, 3), D(2026, 5, 25),
                      D(2026, 6, 19), D(2026, 7, 3), D(2026, 9, 7), D(2026, 11, 26), D(2026, 12, 25)}
CBOE_2026_STANDARD = [D(2026, 1, 16), D(2026, 2, 20), D(2026, 3, 20), D(2026, 4, 17), D(2026, 5, 15),
                      D(2026, 6, 18), D(2026, 7, 17), D(2026, 8, 21), D(2026, 9, 18), D(2026, 10, 16),
                      D(2026, 11, 20), D(2026, 12, 18)]
CBOE_2026_VIX = [D(2026, 1, 21), D(2026, 2, 18), D(2026, 3, 18), D(2026, 4, 15), D(2026, 5, 19),
                 D(2026, 6, 17), D(2026, 7, 22), D(2026, 8, 19), D(2026, 9, 16), D(2026, 10, 21),
                 D(2026, 11, 18), D(2026, 12, 16)]


class TestCalendar(unittest.TestCase):
    def test_2026_holidays_match_cboe_calendar(self):
        self.assertEqual(set(ed.exchange_holidays(2026)), CBOE_2026_HOLIDAYS)

    def test_new_year_on_saturday_is_not_observed_on_dec_31(self):
        self.assertNotIn(D(2021, 12, 31), ed.exchange_holidays(2021))
        self.assertNotIn(D(2022, 1, 1), ed.exchange_holidays(2022))

    def test_shift_skips_weekends_and_holidays(self):
        self.assertEqual(ed.shift_business_days(D(2026, 12, 28), -1), D(2026, 12, 24))
        self.assertEqual(ed.shift_business_days(D(2026, 11, 25), 1), D(2026, 11, 27))

    def test_last_business_day(self):
        self.assertEqual(ed.last_business_day(2026, 12), D(2026, 12, 31))
        self.assertEqual(ed.last_business_day(2026, 5), D(2026, 5, 29))


class TestOptions(unittest.TestCase):
    def test_standard_expiration_matches_cboe_2026(self):
        self.assertEqual([ed.standard_expiration(2026, m) for m in range(1, 13)], CBOE_2026_STANDARD)

    def test_vix_expiration_matches_cboe_2026(self):
        self.assertEqual([ed.vix_expiration(2026, m) for m in range(1, 13)], CBOE_2026_VIX)

    def test_december_vix_uses_next_year_january(self):
        self.assertEqual(ed.vix_expiration(2026, 12), D(2026, 12, 16))


class TestFutures(unittest.TestCase):
    def _contract(self, symbol):
        product = symbol[:2]
        return next(c for c in ed.contracts(product, 2025, 3) if c.symbol == symbol)

    def test_crude_last_trade(self):
        # 25th is a business day (Nov 25 2025 Tue) -> 3 business days before it.
        self.assertEqual(self._contract("CLZ25").last_trade, D(2025, 11, 20))
        # Nov 25 2026 is a Wednesday business day; Thanksgiving is the 26th.
        self.assertEqual(self._contract("CLZ26").last_trade, D(2026, 11, 20))

    def test_crude_when_25th_is_not_a_business_day(self):
        # Jan 2027 contract: Dec 25 2026 is a holiday -> start from Dec 24, then 3 days back.
        self.assertEqual(self._contract("CLF27").last_trade, D(2026, 12, 21))

    def test_gold_dates(self):
        c = self._contract("GCZ25")
        self.assertEqual(c.last_trade, D(2025, 12, 29))      # third-last business day
        self.assertEqual(c.roll_date, D(2025, 11, 28))       # last business day of November
        self.assertEqual(c.roll_label, "first notice day")

    def test_treasury_dates(self):
        c = self._contract("ZNZ25")
        self.assertEqual(c.last_trade, D(2025, 12, 19))      # 7 business days before Dec 31
        self.assertEqual(c.roll_date, D(2025, 11, 26))       # 2 business days before Dec 1

    def test_contract_months(self):
        self.assertEqual({c.month for c in ed.contracts("GC", 2026, 1)}, {2, 4, 6, 8, 10, 12})
        self.assertEqual({c.month for c in ed.contracts("SI", 2026, 1)}, {3, 5, 7, 9, 12})
        self.assertEqual({c.month for c in ed.contracts("CL", 2026, 1)}, set(range(1, 13)))


class TestTables(unittest.TestCase):
    def test_contract_table_has_exactly_one_chart_this_per_product(self):
        today = D(2026, 11, 22)
        rows = ed.contract_table(today)
        for name, _, _ in ed.PRODUCTS.values():
            statuses = [r["status"] for r in rows if r["product"] == name]
            self.assertEqual(statuses.count("Chart this"), 1, name)
            self.assertNotIn("Next", statuses[:statuses.index("Chart this")])

    def test_contract_table_never_lists_expired_contracts(self):
        today = D(2026, 11, 22)
        self.assertTrue(all(r["last_trade"] >= today for r in ed.contract_table(today)))

    def test_rolled_off_contract_is_labelled_until_it_expires(self):
        # Dec 1 2026: GCZ26 first notice (Nov 30) has passed but it has not expired.
        rows = {r["contract"]: r["status"] for r in ed.contract_table(D(2026, 12, 1))}
        self.assertEqual(rows["GCZ26"], "Rolling off")
        self.assertEqual(rows["GCG27"], "Chart this")

    def test_comex_options_expiry_matches_cme_listing(self):
        self.assertEqual(ed.comex_options_expiry(2026, 12), D(2026, 11, 24))   # CME: Nov 24

    def test_comex_options_expiry_never_friday_or_before_a_holiday(self):
        for year in (2026, 2027, 2028):
            for month in (2, 4, 6, 8, 10, 12):
                d = ed.comex_options_expiry(year, month)
                nxt = d + dt.timedelta(days=1)
                self.assertTrue(ed.is_business_day(d))
                self.assertNotEqual(d.weekday(), 4)
                self.assertFalse(nxt.weekday() < 5 and not ed.is_business_day(nxt), (year, month, d))
                self.assertLess(d, ed.last_business_day(year - (month == 1), (month - 2) % 12 + 1))

    def test_gold_roll_window_runs_from_options_expiry_to_first_notice(self):
        def statuses(today):
            return {r["contract"]: r["status"] for r in ed.contract_table(today)}
        before = statuses(D(2026, 11, 23))
        self.assertEqual((before["GCZ26"], before["GCG27"]), ("Chart this", "Next"))
        for day in (D(2026, 11, 24), D(2026, 11, 27), D(2026, 11, 30)):
            window = statuses(day)
            self.assertEqual((window["GCZ26"], window["GCG27"], window["GCJ27"]),
                             ("Roll window", "Switch to this", "Next"), day)
        after = statuses(D(2026, 12, 1))
        self.assertEqual((after["GCZ26"], after["GCG27"]), ("Rolling off", "Chart this"))

    def test_options_expiry_matches_cme_advisory_example(self):
        self.assertEqual(ed.comex_options_expiry(2013, 10), D(2013, 9, 25))   # Chadv13-435

    def test_silver_roll_window_mirrors_gold(self):
        def statuses(today):
            return {r["contract"]: r["status"] for r in ed.contract_table(today)}
        self.assertEqual(statuses(D(2026, 11, 23))["SIZ26"], "Chart this")
        for day in (D(2026, 11, 24), D(2026, 11, 30)):
            window = statuses(day)
            self.assertEqual((window["SIZ26"], window["SIH27"], window["SIK27"]),
                             ("Roll window", "Switch to this", "Next"), day)
        after = statuses(D(2026, 12, 1))
        self.assertEqual((after["SIZ26"], after["SIH27"]), ("Rolling off", "Chart this"))
        events = [e["event"] for e in ed.upcoming_events(D(2026, 11, 20), 30)]
        self.assertIn("COMEX silver options expiry (SIZ26)", events)

    def test_roll_window_not_applied_to_crude_or_treasuries(self):
        rows = {r["contract"]: r["status"] for r in ed.contract_table(D(2026, 11, 24))}
        self.assertEqual(rows["CLF27"], "Chart this")
        self.assertEqual(rows["ZNZ26"], "Chart this")

    def test_comex_options_expiry_listed_in_upcoming_events(self):
        events = ed.upcoming_events(D(2026, 11, 20), 30)
        match = [e for e in events if e["event"] == "COMEX gold options expiry (GCZ26)"]
        self.assertEqual([(e["date"], e["category"]) for e in match], [(D(2026, 11, 24), "Options")])

    def test_upcoming_events_window_and_order(self):
        today = D(2026, 12, 1)
        events = ed.upcoming_events(today, 30)
        dates = [e["date"] for e in events]
        self.assertEqual(dates, sorted(dates))
        self.assertTrue(all(today <= d <= D(2026, 12, 31) for d in dates))
        by_event = {e["event"]: e["date"] for e in events}
        self.assertEqual(by_event["Quarterly expiration (triple witching)"], D(2026, 12, 18))
        self.assertEqual(by_event["VIX options expiration"], D(2026, 12, 16))
        self.assertEqual(by_event["US exchanges closed: Christmas"], D(2026, 12, 25))
        self.assertEqual(by_event["GCZ26 last trading day"], D(2026, 12, 29))

    def test_roll_marker_not_duplicated_for_crude(self):
        events = [e for e in ed.upcoming_events(D(2026, 11, 1), 40) if e["event"].startswith("CLZ26")]
        self.assertEqual([e["event"] for e in events], ["CLZ26 last trading day"])


if __name__ == "__main__":
    unittest.main()
