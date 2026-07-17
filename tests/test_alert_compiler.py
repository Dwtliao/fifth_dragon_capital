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
    def __init__(self, managed_rows=None, prunable_rows=None, reclassify_row=None):
        self.managed_rows = managed_rows or []
        self.prunable_rows = prunable_rows or []
        self.reclassify_row = reclassify_row
        self.executed = []
        self.rowcount = 0
        self.description = []
        self._fetchall_rows = []
        self._fetchone_row = None

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
        elif sql_l.startswith("select id, ticker, label, condition, threshold::float, tier, expires_at, pinned, enabled, archived_at from price_alerts where source = %s and source_key = %s"):
            cols = ["id", "ticker", "label", "condition", "threshold", "tier", "expires_at", "pinned", "enabled", "archived_at"]
            self.description = [_Col(name) for name in cols]
            row = self.reclassify_row
            if row and row.get("source") == params[0] and row.get("source_key") == params[1]:
                self._fetchone_row = tuple(row.get(name) for name in cols)
            else:
                self._fetchone_row = None
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

    def fetchone(self):
        return self._fetchone_row


class _FakeConnection:
    def __init__(self, managed_rows=None, prunable_rows=None, reclassify_row=None):
        self.cursor_obj = _FakeCursor(managed_rows=managed_rows, prunable_rows=prunable_rows, reclassify_row=reclassify_row)
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


class _FakeJournalCursor:
    """Stateful fake supporting the journal match/upsert/promote/reclassify query surface."""

    def __init__(self, db, next_id):
        self.db = db
        self.next_id = next_id
        self.executed = []
        self.description = []
        self._fetchall_rows = []
        self._fetchone_row = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        sql_l = " ".join(sql.lower().split())

        if sql_l.startswith(
            "select id, ticker, label, condition, threshold::float, tier, expires_at, "
            "pinned, enabled, archived_at, source_key, recurrence_count, first_seen_at, last_seen_at, refreshed_at "
            "from price_alerts where source = 'journal_sync'"
        ):
            ticker, condition = params
            rows = [
                r for r in self.db
                if r["source"] == "journal_sync" and not r["pinned"] and r["enabled"]
                and r["archived_at"] is None and r["ticker"] == ticker and r["condition"] == condition
            ]
            rows.sort(key=lambda r: (r["refreshed_at"] or datetime.min.replace(tzinfo=timezone.utc), r["id"]), reverse=True)
            cols = ["id", "ticker", "label", "condition", "threshold", "tier", "expires_at",
                    "pinned", "enabled", "archived_at", "source_key", "recurrence_count",
                    "first_seen_at", "last_seen_at", "refreshed_at"]
            self.description = [_Col(c) for c in cols]
            self._fetchall_rows = [tuple(r.get(c) for c in cols) for r in rows]

        elif sql_l.startswith("insert into price_alerts"):
            ticker, label, condition, threshold, source_key, expires_at, refreshed_at, first_seen_at, last_seen_at = params
            row = {
                "id": self.next_id, "ticker": ticker, "label": label, "condition": condition,
                "threshold": threshold, "source": "journal_sync", "source_key": source_key,
                "tier": 3, "expires_at": expires_at, "pinned": False, "refreshed_at": refreshed_at,
                "enabled": True, "archived_at": None, "triggered": False,
                "recurrence_count": 1, "first_seen_at": first_seen_at, "last_seen_at": last_seen_at,
            }
            self.db.append(row)
            self.next_id += 1

        elif sql_l.startswith("update price_alerts set label = %s, threshold = %s"):
            label, threshold, expires_at, refreshed_at, last_seen_at, row_id = params
            for r in self.db:
                if r["id"] == row_id:
                    r.update(label=label, threshold=threshold, expires_at=expires_at,
                              refreshed_at=refreshed_at, last_seen_at=last_seen_at)
                    r["recurrence_count"] += 1

        elif sql_l.startswith("select source_key, ticker, condition from price_alerts where source = 'journal_sync'"):
            (min_recurrence,) = params
            rows = [
                r for r in self.db
                if r["source"] == "journal_sync" and not r["pinned"] and r["enabled"]
                and r["archived_at"] is None and r["recurrence_count"] >= min_recurrence
            ]
            cols = ["source_key", "ticker", "condition"]
            self.description = [_Col(c) for c in cols]
            self._fetchall_rows = [tuple(r.get(c) for c in cols) for r in rows]

        elif sql_l.startswith("select id, ticker, label, condition, threshold::float, tier, expires_at, pinned, enabled, archived_at from price_alerts where source = %s and source_key = %s"):
            source, source_key = params
            matches = [r for r in self.db if r["source"] == source and r["source_key"] == source_key]
            matches.sort(key=lambda r: r["id"], reverse=True)
            cols = ["id", "ticker", "label", "condition", "threshold", "tier", "expires_at", "pinned", "enabled", "archived_at"]
            self._fetchone_row = tuple(matches[0].get(c) for c in cols) if matches else None
            self.description = [_Col(c) for c in cols]

        elif sql_l.startswith("update price_alerts set source = %s, source_key = %s"):
            (source, source_key, ticker, label, condition, threshold, tier,
             expires_at, pinned, refreshed_at, row_id) = params
            for r in self.db:
                if r["id"] == row_id:
                    r.update(source=source, source_key=source_key, ticker=ticker, label=label,
                              condition=condition, threshold=threshold, tier=tier, expires_at=expires_at,
                              pinned=pinned, refreshed_at=refreshed_at, enabled=True, archived_at=None,
                              triggered=False)

        elif sql_l.startswith("update price_alerts set expires_at = null where source = 'key_levels_watch'"):
            (source_key,) = params
            for r in self.db:
                if r["source"] == "key_levels_watch" and r["source_key"] == source_key:
                    r["expires_at"] = None

        elif sql_l.startswith("select 1 from price_alerts where source = 'key_levels_watch' and source_key = %s"):
            (source_key,) = params
            self._fetchone_row = (1,) if any(
                r["source"] == "key_levels_watch" and r["source_key"] == source_key for r in self.db
            ) else None

        elif sql_l.startswith("select id from price_alerts where source = %s and source_key = %s"):
            source, source_key = params
            matches = [r for r in self.db if r["source"] == source and r["source_key"] == source_key]
            self._fetchone_row = (matches[0]["id"],) if matches else None

        elif sql_l.startswith("select id, ticker, source, condition, threshold::float, label from price_alerts where enabled = true and archived_at is null"):
            rows = [r for r in self.db if r["enabled"] and r["archived_at"] is None]
            rows.sort(key=lambda r: (r["ticker"], r["condition"], r["threshold"]))
            cols = ["id", "ticker", "source", "condition", "threshold", "label"]
            self.description = [_Col(c) for c in cols]
            self._fetchall_rows = [tuple(r.get(c) for c in cols) for r in rows]

        elif sql_l.startswith("select threshold::float from price_alerts where source = 'key_levels_watch' and enabled = true and archived_at is null and ticker = %s and condition = %s"):
            ticker, condition = params
            matches = [
                r for r in self.db
                if r["source"] == "key_levels_watch" and r["enabled"] and r["archived_at"] is None
                and r["ticker"] == ticker and r["condition"] == condition
            ]
            self._fetchall_rows = [(r["threshold"],) for r in matches]

        elif sql_l.startswith("update price_alerts set enabled = false, triggered = false, archived_at = %s, refreshed_at = %s where id = %s"):
            archived_at, refreshed_at, row_id = params
            for r in self.db:
                if r["id"] == row_id:
                    r.update(enabled=False, triggered=False, archived_at=archived_at, refreshed_at=refreshed_at)

    def fetchall(self):
        return self._fetchall_rows

    def fetchone(self):
        return self._fetchone_row


class _FakeJournalConnection:
    """Shares one mutable row list across every get_connection() call in a test."""

    def __init__(self, rows=None, next_id=1):
        self.db = rows if rows is not None else []
        self.next_id = next_id
        self.committed = False
        self.rolled_back = False

    @contextmanager
    def cursor(self):
        cur = _FakeJournalCursor(self.db, self.next_id)
        yield cur
        self.next_id = cur.next_id

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class _FakeStaleCursor:
    """Fake cursor answering the two-query surface find_stale_alerts() issues:
    the manual-alert scan against price_alerts, then the open-positions lookup
    against mv_unrealized_pnl."""

    def __init__(self, alert_rows=None, open_symbols=None):
        self.alert_rows = alert_rows or []
        self.open_symbols = open_symbols or set()
        self.executed = []
        self.description = []
        self._fetchall_rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        sql_l = " ".join(sql.lower().split())
        if sql_l.startswith("select id, ticker, label, condition, threshold::float, created_at from price_alerts"):
            (cutoff,) = params
            cols = ["id", "ticker", "label", "condition", "threshold", "created_at"]
            self.description = [_Col(c) for c in cols]
            matches = [
                r for r in self.alert_rows
                if r["source"] == "manual" and r["enabled"] and r["archived_at"] is None
                and r["last_fired_at"] is None and r["created_at"] <= cutoff
            ]
            matches.sort(key=lambda r: r["created_at"])
            self._fetchall_rows = [tuple(r.get(c) for c in cols) for r in matches]
        elif sql_l.startswith("select distinct symbol from mv_unrealized_pnl"):
            self.description = [_Col("symbol")]
            self._fetchall_rows = [(s,) for s in self.open_symbols]
        else:
            self._fetchall_rows = []

    def fetchall(self):
        return self._fetchall_rows


class _FakeStaleConnection:
    def __init__(self, alert_rows=None, open_symbols=None):
        self.cursor_obj = _FakeStaleCursor(alert_rows=alert_rows, open_symbols=open_symbols)

    @contextmanager
    def cursor(self):
        yield self.cursor_obj

    def commit(self):
        pass

    def rollback(self):
        pass

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

    def test_build_structural_alerts_skips_redundant_alert_above_when_equal_to_resistance(self):
        key_levels = {
            "positions": {},
            "watch": {"^VIX": {"resistance": 20.0, "alert_above": 20.0}},
        }

        alerts = build_structural_alerts(key_levels)
        source_keys = {row["source_key"] for row in alerts}

        # only one "above 20" alert, not two identical ones
        self.assertEqual(source_keys, {"watch:^VIX:resistance"})

    def test_build_structural_alerts_keeps_distinct_resistance_and_alert_above(self):
        key_levels = {
            "positions": {},
            "watch": {"^VIX": {"resistance": 20.0, "alert_above": 22.5}},
        }

        alerts = build_structural_alerts(key_levels)
        source_keys = {row["source_key"] for row in alerts}

        self.assertEqual(source_keys, {"watch:^VIX:resistance", "watch:^VIX:alert_above"})

    def test_find_duplicate_alerts_clusters_cross_source_near_matches(self):
        now = datetime.now(timezone.utc)
        conn = _FakeJournalConnection(rows=[
            {"id": 1, "ticker": "^VIX", "source": "key_levels_watch", "condition": "above",
             "threshold": 20.0, "label": "Resistance", "enabled": True, "archived_at": None,
             "source_key": "watch:^VIX:resistance", "pinned": False, "recurrence_count": 1,
             "first_seen_at": now, "last_seen_at": now, "refreshed_at": now},
            {"id": 2, "ticker": "^VIX", "source": "manual", "condition": "above",
             "threshold": 20.0, "label": "Vol playbook re-entry", "enabled": True, "archived_at": None,
             "source_key": None, "pinned": False, "recurrence_count": 1,
             "first_seen_at": None, "last_seen_at": None, "refreshed_at": now},
            {"id": 3, "ticker": "^VIX", "source": "manual", "condition": "above",
             "threshold": 45.0, "label": "Crisis-level spike", "enabled": True, "archived_at": None,
             "source_key": None, "pinned": False, "recurrence_count": 1,
             "first_seen_at": None, "last_seen_at": None, "refreshed_at": now},
        ], next_id=4)

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import find_duplicate_alerts
            clusters = find_duplicate_alerts()

        self.assertEqual(len(clusters), 1)  # only the 20.0 pair clusters; 45.0 is far away
        cluster = clusters[0]
        self.assertEqual(cluster["ticker"], "^VIX")
        self.assertEqual({r["id"] for r in cluster["rows"]}, {1, 2})
        self.assertEqual({r["source"] for r in cluster["rows"]}, {"key_levels_watch", "manual"})

    def test_find_duplicate_alerts_ignores_archived_and_disabled_rows(self):
        now = datetime.now(timezone.utc)
        conn = _FakeJournalConnection(rows=[
            {"id": 1, "ticker": "GC=F", "source": "key_levels_watch", "condition": "below",
             "threshold": 4000.0, "label": "Support", "enabled": True, "archived_at": None,
             "source_key": "watch:GC=F:support", "pinned": False, "recurrence_count": 1,
             "first_seen_at": now, "last_seen_at": now, "refreshed_at": now},
            {"id": 2, "ticker": "GC=F", "source": "journal_sync", "condition": "below",
             "threshold": 4005.0, "label": "Gold loses support", "enabled": False, "archived_at": now,
             "source_key": "journal:GC=F:below:x", "pinned": False, "recurrence_count": 1,
             "first_seen_at": now, "last_seen_at": now, "refreshed_at": now},
        ], next_id=3)

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import find_duplicate_alerts
            clusters = find_duplicate_alerts()

        self.assertEqual(clusters, [])  # the archived row is excluded, so there's no active pair to cluster

    def test_build_journal_alerts_normalizes_without_keying(self):
        alerts = build_journal_alerts([
            {"ticker": "NQ=F", "condition": "above", "threshold": 30200, "label": "Breakout"},
            {"ticker": "bad", "condition": "sideways", "threshold": 1},  # invalid condition, dropped
        ])

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertNotIn("source_key", alert)
        self.assertNotIn("expires_at", alert)
        self.assertEqual(alert["ticker"], "NQ=F")
        self.assertEqual(alert["tier"], 3)
        self.assertEqual(alert["condition"], "above")
        self.assertEqual(alert["threshold"], 30200.0)
        self.assertEqual(alert["label"], "Breakout")

    def test_journal_match_tolerance_is_percent_clamped(self):
        from morning_brief.alert_compiler import _journal_match_tolerance, JOURNAL_MATCH_MIN_ABS, JOURNAL_MATCH_MAX_ABS

        # cheap stock: 1% would be tiny, clamped up to the floor
        self.assertAlmostEqual(_journal_match_tolerance(2.0), JOURNAL_MATCH_MIN_ABS)
        # expensive future: 1% would be huge, clamped down to the ceiling
        self.assertAlmostEqual(_journal_match_tolerance(30000.0), JOURNAL_MATCH_MAX_ABS)
        # mid-range: plain 1%
        self.assertAlmostEqual(_journal_match_tolerance(1000.0), 10.0)

    def test_reconcile_journal_alerts_updates_recurrence_within_tolerance(self):
        conn = _FakeJournalConnection()

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import reconcile_journal_alerts

            stats1 = reconcile_journal_alerts([
                {"ticker": "GC=F", "condition": "below", "threshold": 4200, "label": "Gold re-entry"},
            ])
            self.assertEqual(stats1["created"], 1)
            self.assertEqual(stats1["updated"], 0)
            self.assertEqual(len(conn.db), 1)
            self.assertEqual(conn.db[0]["recurrence_count"], 1)

            # same idea, slightly different threshold (within 1% band, well under $25 clamp)
            stats2 = reconcile_journal_alerts([
                {"ticker": "GC=F", "condition": "below", "threshold": 4180, "label": "Gold re-entry (updated)"},
            ])
            self.assertEqual(stats2["created"], 0)
            self.assertEqual(stats2["updated"], 1)
            self.assertEqual(len(conn.db), 1)  # still one row, matched not duplicated
            self.assertEqual(conn.db[0]["recurrence_count"], 2)
            self.assertEqual(conn.db[0]["threshold"], 4180.0)

    def test_reconcile_journal_alerts_creates_new_row_outside_tolerance(self):
        conn = _FakeJournalConnection()

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import reconcile_journal_alerts

            reconcile_journal_alerts([
                {"ticker": "CL=F", "condition": "above", "threshold": 80, "label": "Resistance"},
            ])
            # 83 vs 80 is $3 away — beyond the $25 max clamp band? No: 1% of 80 = 0.80,
            # clamped up to the $0.05 floor... clamp min applies only when pct is too small.
            # 1% of 80 = 0.80 (already above the floor), so anything beyond ~$0.80 is a new idea.
            stats2 = reconcile_journal_alerts([
                {"ticker": "CL=F", "condition": "above", "threshold": 83, "label": "Breakout above resistance cluster"},
            ])

        self.assertEqual(stats2["created"], 1)
        self.assertEqual(stats2["updated"], 0)
        self.assertEqual(len(conn.db), 2)  # two distinct ideas, not merged

    def test_reconcile_journal_alerts_never_updates_manual_rows(self):
        now = datetime.now(timezone.utc)
        conn = _FakeJournalConnection(rows=[{
            "id": 1, "source": "manual", "source_key": None, "ticker": "GC=F",
            "label": "manual gold alert", "condition": "below", "threshold": 4200.0,
            "pinned": True, "enabled": True, "archived_at": None,
            "recurrence_count": 1, "first_seen_at": now, "last_seen_at": now, "refreshed_at": now,
        }], next_id=2)

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import reconcile_journal_alerts
            stats = reconcile_journal_alerts([
                {"ticker": "GC=F", "condition": "below", "threshold": 4195, "label": "Gold re-entry"},
            ])

        # a manual alert at the same price doesn't block or absorb the new journal idea —
        # only structural alerts do (manual intent is ambiguous, structural is authoritative)
        self.assertEqual(stats["created"], 1)
        self.assertEqual(stats["skipped_structural"], 0)
        self.assertEqual(len(conn.db), 2)
        self.assertEqual(conn.db[0]["recurrence_count"], 1)  # manual row untouched

    def test_reconcile_journal_alerts_skips_when_structural_alert_already_covers_it(self):
        now = datetime.now(timezone.utc)
        conn = _FakeJournalConnection(rows=[{
            "id": 1, "source": "key_levels_watch", "source_key": "watch:GC=F:support", "ticker": "GC=F",
            "label": "Support: GC=F below 4200", "condition": "below", "threshold": 4200.0,
            "pinned": False, "enabled": True, "archived_at": None,
            "recurrence_count": 1, "first_seen_at": now, "last_seen_at": now, "refreshed_at": now,
        }], next_id=2)

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import reconcile_journal_alerts
            stats = reconcile_journal_alerts([
                {"ticker": "GC=F", "condition": "below", "threshold": 4195, "label": "Gold re-entry"},
            ])

        # already covered structurally — no tactical shadow row gets created
        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["skipped_structural"], 1)
        self.assertEqual(len(conn.db), 1)
        self.assertEqual(conn.db[0]["recurrence_count"], 1)  # structural row untouched, not "matched" either

    def test_promotion_reclassifies_after_threshold_recurrences(self):
        now = datetime.now(timezone.utc)
        conn = _FakeJournalConnection(rows=[{
            "id": 5, "source": "journal_sync", "source_key": "journal:XLE:above:20260101T000000000000",
            "ticker": "XLE", "label": "XLE breakout", "condition": "above", "threshold": 58.0,
            "pinned": False, "enabled": True, "archived_at": None,
            "recurrence_count": 3, "first_seen_at": now, "last_seen_at": now, "refreshed_at": now,
            "expires_at": now + timedelta(days=10),
        }], next_id=6)

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import promote_recurring_journal_alerts
            promoted = promote_recurring_journal_alerts()

        self.assertEqual(promoted, 1)
        row = conn.db[0]
        self.assertEqual(row["source"], "key_levels_watch")
        self.assertEqual(row["source_key"], "watch:XLE:resistance")
        self.assertEqual(row["tier"], 2)
        self.assertIsNone(row["expires_at"])  # TTL cleared on promotion — structural alerts are long-lived

    def test_promotion_archives_journal_alert_instead_of_colliding_with_existing_structural(self):
        now = datetime.now(timezone.utc)
        conn = _FakeJournalConnection(rows=[
            {
                "id": 5, "source": "journal_sync", "source_key": "journal:XLE:above:x",
                "ticker": "XLE", "label": "XLE breakout", "condition": "above", "threshold": 58.6,
                "pinned": False, "enabled": True, "archived_at": None,
                "recurrence_count": 3, "first_seen_at": now, "last_seen_at": now, "refreshed_at": now,
                "expires_at": now + timedelta(days=10),
            },
            {
                # key_levels.yml already has a structural resistance alert at this slot
                "id": 6, "source": "key_levels_watch", "source_key": "watch:XLE:resistance",
                "ticker": "XLE", "label": "Resistance: XLE above 58", "condition": "above", "threshold": 58.0,
                "pinned": False, "enabled": True, "archived_at": None,
                "recurrence_count": 1, "first_seen_at": None, "last_seen_at": None, "refreshed_at": now,
                "expires_at": None,
            },
        ], next_id=7)

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import promote_recurring_journal_alerts
            promoted = promote_recurring_journal_alerts()

        self.assertEqual(promoted, 1)
        journal_row = next(r for r in conn.db if r["id"] == 5)
        structural_row = next(r for r in conn.db if r["id"] == 6)
        # journal duplicate retired, not merged into (structural stays authoritative from key_levels.yml)
        self.assertFalse(journal_row["enabled"])
        self.assertIsNotNone(journal_row["archived_at"])
        self.assertEqual(journal_row["source"], "journal_sync")  # never reclassified — would've collided
        self.assertEqual(structural_row["threshold"], 58.0)  # untouched

    def test_promotion_leaves_alerts_below_threshold_alone(self):
        now = datetime.now(timezone.utc)
        conn = _FakeJournalConnection(rows=[{
            "id": 7, "source": "journal_sync", "source_key": "journal:XLE:above:x",
            "ticker": "XLE", "label": "XLE breakout", "condition": "above", "threshold": 58.0,
            "pinned": False, "enabled": True, "archived_at": None,
            "recurrence_count": 2, "first_seen_at": now, "last_seen_at": now, "refreshed_at": now,
            "expires_at": now + timedelta(days=10),
        }], next_id=8)

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import promote_recurring_journal_alerts
            promoted = promote_recurring_journal_alerts()

        self.assertEqual(promoted, 0)
        self.assertEqual(conn.db[0]["source"], "journal_sync")

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

    def test_reclassify_moves_alert_across_source_boundary(self):
        now = datetime.now(timezone.utc)
        conn = _FakeConnection(reclassify_row={
            "id": 42,
            "source": "journal_sync",
            "source_key": "journal:NQ=F:above",
            "ticker": "NQ=F",
            "label": "Breakout",
            "condition": "above",
            "threshold": 30200.0,
            "tier": 3,
            "expires_at": now + timedelta(days=5),
            "pinned": False,
            "enabled": True,
            "archived_at": None,
        })

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import reclassify
            moved = reclassify(
                "journal_sync", "journal:NQ=F:above",
                "key_levels_watch", "watch:NQ=F:alert_above",
                tier=2,
            )

        self.assertTrue(moved)
        self.assertTrue(conn.committed)
        update_sql, update_params = next(
            (sql, params) for sql, params in conn.cursor_obj.executed if sql.strip().lower().startswith("update")
        )
        self.assertIn("key_levels_watch", update_params)
        self.assertIn("watch:NQ=F:alert_above", update_params)
        self.assertIn(2, update_params)  # new tier
        self.assertIn(30200.0, update_params)  # threshold carried over unchanged
        self.assertIn(42, update_params)  # WHERE id = 42

    def test_reclassify_returns_false_when_source_key_not_found(self):
        conn = _FakeConnection(reclassify_row=None)

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import reclassify
            moved = reclassify("journal_sync", "journal:MISSING:above", "key_levels_watch", "watch:MISSING:alert_above")

        self.assertFalse(moved)

    def test_refresh_structural_alerts_from_db_wires_fetchers_into_compiler(self):
        conn = _FakeConnection(managed_rows=[])
        key_levels = {"positions": {}, "watch": {"SPY": {"alert_above": 505.0}}}

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn), \
             patch("morning_brief.fetchers.load_key_levels_from_db", return_value=key_levels):
            from morning_brief.alert_compiler import refresh_structural_alerts_from_db
            stats = refresh_structural_alerts_from_db()

        self.assertEqual(stats["created"], 1)
        self.assertEqual(stats["archived"], 0)
        self.assertEqual(stats["backfilled"], 0)
        self.assertTrue(conn.committed)

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

    def test_find_stale_alerts_reports_old_never_fired_manual_alert(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=120)
        conn = _FakeStaleConnection(alert_rows=[
            {"id": 1, "ticker": "NQ=F", "label": "Breakout watch", "condition": "above",
             "threshold": 22000.0, "source": "manual", "enabled": True, "archived_at": None,
             "last_fired_at": None, "created_at": old},
        ], open_symbols=set())

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import find_stale_alerts
            report = find_stale_alerts()

        self.assertEqual(len(report), 1)
        row = report[0]
        self.assertEqual(row["id"], 1)
        self.assertEqual(row["source"], "manual")
        self.assertGreaterEqual(row["age_days"], 119)
        self.assertFalse(row["has_open_position"])
        self.assertEqual(row["confidence"], "no_open_position")

    def test_find_stale_alerts_excludes_recently_created_manual_alert(self):
        now = datetime.now(timezone.utc)
        recent = now - timedelta(days=5)
        conn = _FakeStaleConnection(alert_rows=[
            {"id": 2, "ticker": "AAPL", "label": "Stop watch", "condition": "below",
             "threshold": 180.0, "source": "manual", "enabled": True, "archived_at": None,
             "last_fired_at": None, "created_at": recent},
        ])

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import find_stale_alerts
            report = find_stale_alerts()

        self.assertEqual(report, [])

    def test_find_stale_alerts_excludes_fired_manual_alert(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=120)
        conn = _FakeStaleConnection(alert_rows=[
            {"id": 3, "ticker": "VIXY", "label": "Fear spike", "condition": "above",
             "threshold": 25.0, "source": "manual", "enabled": True, "archived_at": None,
             "last_fired_at": old + timedelta(days=1), "created_at": old},
        ])

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import find_stale_alerts
            report = find_stale_alerts()

        self.assertEqual(report, [])

    def test_find_stale_alerts_excludes_archived_and_disabled_rows(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=120)
        conn = _FakeStaleConnection(alert_rows=[
            {"id": 4, "ticker": "DX-Y.NYB", "label": "Dollar watch", "condition": "above",
             "threshold": 110.0, "source": "manual", "enabled": False, "archived_at": None,
             "last_fired_at": None, "created_at": old},
            {"id": 5, "ticker": "CL=F", "label": "Oil watch", "condition": "below",
             "threshold": 60.0, "source": "manual", "enabled": True, "archived_at": old,
             "last_fired_at": None, "created_at": old},
        ])

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import find_stale_alerts
            report = find_stale_alerts()

        self.assertEqual(report, [])

    def test_find_stale_alerts_ignores_non_manual_sources(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=120)
        conn = _FakeStaleConnection(alert_rows=[
            {"id": 6, "ticker": "SPY", "label": "Resistance", "condition": "above",
             "threshold": 510.0, "source": "key_levels_watch", "enabled": True, "archived_at": None,
             "last_fired_at": None, "created_at": old},
            {"id": 7, "ticker": "GC=F", "label": "Gold idea", "condition": "below",
             "threshold": 4200.0, "source": "journal_sync", "enabled": True, "archived_at": None,
             "last_fired_at": None, "created_at": old},
        ])

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import find_stale_alerts
            report = find_stale_alerts()

        self.assertEqual(report, [])

    def test_find_stale_alerts_flags_open_position_as_lower_confidence(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=120)
        conn = _FakeStaleConnection(alert_rows=[
            {"id": 8, "ticker": "NVDA", "label": "Stop watch", "condition": "below",
             "threshold": 130.0, "source": "manual", "enabled": True, "archived_at": None,
             "last_fired_at": None, "created_at": old},
        ], open_symbols={"NVDA"})

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import find_stale_alerts
            report = find_stale_alerts()

        self.assertEqual(len(report), 1)
        self.assertTrue(report[0]["has_open_position"])
        self.assertEqual(report[0]["confidence"], "age_only")

    def test_find_stale_alerts_cutoff_is_configurable(self):
        now = datetime.now(timezone.utc)
        thirty_days_old = now - timedelta(days=30)
        conn = _FakeStaleConnection(alert_rows=[
            {"id": 9, "ticker": "SI=F", "label": "Silver watch", "condition": "above",
             "threshold": 32.0, "source": "manual", "enabled": True, "archived_at": None,
             "last_fired_at": None, "created_at": thirty_days_old},
        ])

        with patch("morning_brief.alert_compiler.get_connection", return_value=conn):
            from morning_brief.alert_compiler import find_stale_alerts
            self.assertEqual(find_stale_alerts(), [])  # default 90-day cutoff, not yet stale
            report = find_stale_alerts(min_age_days=20)  # tighter cutoff catches it

        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["id"], 9)


if __name__ == "__main__":
    unittest.main()
