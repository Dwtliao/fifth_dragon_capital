import os
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "postgresql://localhost/fake")

from morning_brief.alert_compiler import build_journal_alerts, build_structural_alerts


class _Col:
    def __init__(self, name):
        self.name = name


class _FakeCursor:
    def __init__(self, managed_rows=None, prunable_rows=None):
        self.managed_rows = managed_rows or []
        self.prunable_rows = prunable_rows or []
        self.executed = []
        self.rowcount = 0
        self.description = []
        self._fetchall_rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        sql_l = " ".join(sql.lower().split())
        if sql_l.startswith("select id, source, source_key, ticker, label, condition, threshold::float, enabled, triggered, tier, expires_at, archived_at, pinned from price_alerts where source = any"):
            requested_sources = set(params[0]) if params else set()
            cols = ["id", "source", "source_key", "ticker", "label", "condition", "threshold", "enabled", "triggered", "tier", "expires_at", "archived_at", "pinned"]
            self.description = [_Col(name) for name in cols]
            self._fetchall_rows = [
                tuple(row.get(name) for name in cols)
                for row in self.managed_rows
                if row.get("source") in requested_sources
            ]
        elif sql_l.startswith("update price_alerts set enabled = false, triggered = false, archived_at = %s, refreshed_at = %s where enabled = true and expires_at is not null and expires_at <= %s"):
            cutoff = params[2]
            self.rowcount = sum(
                1
                for row in self.prunable_rows
                if row.get("enabled", True) and row.get("expires_at") is not None and row["expires_at"] <= cutoff
            )
        elif sql_l.startswith("update price_alerts set enabled = false, triggered = false, archived_at = %s, refreshed_at = %s where id = %s"):
            self.rowcount = 1
        else:
            self.rowcount = 0

    def fetchall(self):
        return self._fetchall_rows


class _FakeConnection:
    def __init__(self, managed_rows=None, prunable_rows=None):
        self.cursor_obj = _FakeCursor(managed_rows=managed_rows, prunable_rows=prunable_rows)
        self.committed = False
        self.rolled_back = False

    @contextmanager
    def cursor(self):
        yield self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class AlertCompilerTests(unittest.TestCase):
    def test_build_structural_alerts_compiles_all_structural_sources(self):
        key_levels = {
            "positions": {
                "AAPL": {"stop": 195.0, "note": "hold"},
            },
            "watch": {
                "SPY": {
                    "support": 500.0,
                    "resistance": 510.0,
                    "alert_above": 505.0,
                    "note": "watch",
                }
            },
        }

        alerts = build_structural_alerts(key_levels)
        source_keys = {row["source_key"] for row in alerts}

        self.assertEqual(
            source_keys,
            {
                "position:AAPL:stop",
                "watch:SPY:support",
                "watch:SPY:resistance",
                "watch:SPY:alert_above",
            },
        )
        by_key = {row["source_key"]: row for row in alerts}
        self.assertEqual(by_key["position:AAPL:stop"]["tier"], 1)
        self.assertEqual(by_key["watch:SPY:support"]["condition"], "below")
        self.assertEqual(by_key["watch:SPY:resistance"]["condition"], "above")
        self.assertIn("hold", by_key["position:AAPL:stop"]["label"])
        self.assertIn("watch", by_key["watch:SPY:alert_above"]["label"])

    def test_build_journal_alerts_uses_stable_key_and_ttl(self):
        alerts = build_journal_alerts([
            {"ticker": "NQ=F", "condition": "above", "threshold": 30200, "label": "Breakout"},
        ])

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["source_key"], "journal:NQ=F:above")
        self.assertEqual(alert["tier"], 3)
        self.assertEqual(alert["condition"], "above")
        self.assertEqual(alert["threshold"], 30200.0)

        now = datetime.now(timezone.utc)
        self.assertGreater(alert["expires_at"], now)
        self.assertLess(alert["expires_at"], now + timedelta(days=22))

    def test_journal_reconcile_does_not_archive_missing_rows(self):
        now = datetime.now(timezone.utc)
        conn = _FakeConnection(managed_rows=[{
            "id": 1,
            "source": "journal_sync",
            "source_key": "journal:GC=F:below",
            "ticker": "GC=F",
            "label": "old",
            "condition": "below",
            "threshold": 4200.0,
            "enabled": True,
            "triggered": False,
            "tier": 3,
            "expires_at": now + timedelta(days=10),
            "archived_at": None,
            "pinned": False,
        }])

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import reconcile_journal_alerts
            stats = reconcile_journal_alerts([])

        self.assertEqual(stats["archived"], 0)
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["updated"], 0)
        sql_text = "\n".join(sql for sql, _ in conn.cursor_obj.executed).lower()
        self.assertNotIn("set enabled = false", sql_text)
        self.assertTrue(conn.committed)

    def test_structural_reconcile_does_not_touch_journal_rows(self):
        now = datetime.now(timezone.utc)
        conn = _FakeConnection(managed_rows=[
            {
                "id": 1,
                "source": "key_levels_watch",
                "source_key": "watch:XLE:support",
                "ticker": "XLE",
                "label": "Support: XLE below 53",
                "condition": "below",
                "threshold": 53.0,
                "enabled": True,
                "triggered": False,
                "tier": 2,
                "expires_at": None,
                "archived_at": None,
                "pinned": False,
            },
            {
                "id": 2,
                "source": "journal_sync",
                "source_key": "journal:GC=F:below",
                "ticker": "GC=F",
                "label": "old",
                "condition": "below",
                "threshold": 4200.0,
                "enabled": True,
                "triggered": False,
                "tier": 3,
                "expires_at": now + timedelta(days=10),
                "archived_at": None,
                "pinned": False,
            },
        ])

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import reconcile_structural_alerts
            stats = reconcile_structural_alerts({
                "positions": {},
                "watch": {"XLE": {"support": 53.0}},
            })

        self.assertEqual(stats["archived"], 0)
        sql_text = "\n".join(sql for sql, _ in conn.cursor_obj.executed).lower()
        self.assertNotIn("set enabled = false", sql_text)

    def test_prune_expired_alerts_only_archives_past_due_rows(self):
        now = datetime.now(timezone.utc)
        conn = _FakeConnection(prunable_rows=[
            {"enabled": True, "expires_at": now - timedelta(days=1)},
            {"enabled": True, "expires_at": now + timedelta(days=1)},
            {"enabled": False, "expires_at": now - timedelta(days=1)},
        ])

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import prune_expired_alerts
            pruned = prune_expired_alerts()

        self.assertEqual(pruned, 1)
        self.assertTrue(conn.committed)


if __name__ == "__main__":
    unittest.main()
