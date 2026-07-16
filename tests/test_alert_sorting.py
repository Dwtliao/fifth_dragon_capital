import unittest
from datetime import datetime, timedelta, timezone

from dashboard.alert_sorting import alert_distance, sort_active_alerts, sort_archived_alerts

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _alert(id, tier, threshold, ticker="TICK", last_fired_at=None):
    return {
        "id": id, "tier": tier, "threshold": threshold, "ticker": ticker,
        "last_fired_at": last_fired_at,
    }


class TestAlertDistance(unittest.TestCase):
    def test_normalizes_by_price(self):
        self.assertAlmostEqual(alert_distance(100.0, 90.0), 0.10)

    def test_missing_price_sorts_last(self):
        self.assertEqual(alert_distance(None, 90.0), float("inf"))

    def test_zero_price_sorts_last(self):
        self.assertEqual(alert_distance(0.0, 90.0), float("inf"))


class TestSortActiveAlerts(unittest.TestCase):
    def test_tier_takes_priority_over_distance(self):
        tier2_close = _alert(1, tier=2, threshold=99.0, ticker="A")
        tier1_far   = _alert(2, tier=1, threshold=50.0, ticker="B")
        prices = {"A": 100.0, "B": 100.0}
        result = sort_active_alerts([tier2_close, tier1_far], prices)
        self.assertEqual([a["id"] for a in result], [2, 1])

    def test_distance_breaks_tier_tie(self):
        far   = _alert(1, tier=1, threshold=50.0, ticker="A")
        close = _alert(2, tier=1, threshold=99.0, ticker="B")
        prices = {"A": 100.0, "B": 100.0}
        result = sort_active_alerts([far, close], prices)
        self.assertEqual([a["id"] for a in result], [2, 1])

    def test_last_fired_desc_breaks_remaining_tie(self):
        older = _alert(1, tier=1, threshold=90.0, ticker="A", last_fired_at=NOW - timedelta(days=1))
        newer = _alert(2, tier=1, threshold=90.0, ticker="A", last_fired_at=NOW)
        prices = {"A": 100.0}
        result = sort_active_alerts([older, newer], prices)
        self.assertEqual([a["id"] for a in result], [2, 1])

    def test_missing_last_fired_at_treated_as_oldest(self):
        never_fired = _alert(1, tier=1, threshold=90.0, ticker="A", last_fired_at=None)
        fired       = _alert(2, tier=1, threshold=90.0, ticker="A", last_fired_at=NOW)
        prices = {"A": 100.0}
        result = sort_active_alerts([never_fired, fired], prices)
        self.assertEqual([a["id"] for a in result], [2, 1])

    def test_missing_price_sorts_alert_last(self):
        no_price = _alert(1, tier=1, threshold=90.0, ticker="MISSING")
        has_price = _alert(2, tier=1, threshold=90.0, ticker="A")
        prices = {"A": 100.0}
        result = sort_active_alerts([no_price, has_price], prices)
        self.assertEqual([a["id"] for a in result], [2, 1])


class TestSortArchivedAlerts(unittest.TestCase):
    def _archived(self, id, archived_at=None, last_fired_at=None):
        return {"id": id, "archived_at": archived_at, "last_fired_at": last_fired_at}

    def test_sorted_by_archived_at_desc(self):
        older = self._archived(1, archived_at=NOW - timedelta(days=1))
        newer = self._archived(2, archived_at=NOW)
        result = sort_archived_alerts([older, newer])
        self.assertEqual([a["id"] for a in result], [2, 1])

    def test_last_fired_at_breaks_archived_at_tie(self):
        older_fire = self._archived(1, archived_at=NOW, last_fired_at=NOW - timedelta(days=1))
        newer_fire = self._archived(2, archived_at=NOW, last_fired_at=NOW)
        result = sort_archived_alerts([older_fire, newer_fire])
        self.assertEqual([a["id"] for a in result], [2, 1])

    def test_missing_timestamps_treated_as_oldest(self):
        never_archived = self._archived(1, archived_at=None, last_fired_at=None)
        archived       = self._archived(2, archived_at=NOW, last_fired_at=None)
        result = sort_archived_alerts([never_archived, archived])
        self.assertEqual([a["id"] for a in result], [2, 1])


if __name__ == "__main__":
    unittest.main()
