"""Alert compiler/reconciler for price_alerts.

This module keeps source-driven alerts in sync with the price_alerts table
without touching the poller. It reconciles desired alerts from structural
sources (key levels) and journal extraction against the live table.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from etrade_sync.db import get_connection

MANAGED_SOURCES = ("key_levels_watch", "journal_sync")
DEFAULT_JOURNAL_ALERT_TTL_DAYS = 21


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_threshold(value: Any) -> str:
    amount = _as_float(value)
    if amount is None:
        return "0"
    text = f"{amount:,.2f}"
    return text.rstrip("0").rstrip(".")


def _append_unique(desired: list[dict], seen: set[tuple[str, str]], alert: dict) -> None:
    source = alert.get("source")
    source_key = alert.get("source_key")
    if not source or not source_key:
        return
    key = (str(source), str(source_key))
    if key in seen:
        return
    seen.add(key)
    desired.append(alert)


def build_structural_alerts(key_levels: dict) -> list[dict]:
    """Compile structural alerts from positions and watch levels."""
    desired: list[dict] = []
    seen: set[tuple[str, str]] = set()

    positions = dict(key_levels.get("positions") or {})
    watch = dict(key_levels.get("watch") or {})

    for ticker in sorted(positions):
        vals = positions.get(ticker) or {}
        stop = _as_float(vals.get("stop"))
        if stop is None or stop <= 0:
            continue
        note = str(vals.get("note") or "").strip()
        label = f"Stop: {ticker} below {_format_threshold(stop)}"
        if note:
            label = f"{label} — {note}"
        _append_unique(desired, seen, {
            "source": "key_levels_watch",
            "source_key": f"position:{ticker}:stop",
            "tier": 1,
            "ticker": ticker,
            "label": label,
            "condition": "below",
            "threshold": stop,
            "expires_at": None,
            "pinned": False,
        })

    for ticker in sorted(watch):
        vals = watch.get(ticker) or {}
        note = str(vals.get("note") or "").strip()

        support = _as_float(vals.get("support"))
        if support is not None and support > 0:
            label = f"Support: {ticker} below {_format_threshold(support)}"
            if note:
                label = f"{label} — {note}"
            _append_unique(desired, seen, {
                "source": "key_levels_watch",
                "source_key": f"watch:{ticker}:support",
                "tier": 2,
                "ticker": ticker,
                "label": label,
                "condition": "below",
                "threshold": support,
                "expires_at": None,
                "pinned": False,
            })

        resistance = _as_float(vals.get("resistance"))
        if resistance is not None and resistance > 0:
            label = f"Resistance: {ticker} above {_format_threshold(resistance)}"
            if note:
                label = f"{label} — {note}"
            _append_unique(desired, seen, {
                "source": "key_levels_watch",
                "source_key": f"watch:{ticker}:resistance",
                "tier": 2,
                "ticker": ticker,
                "label": label,
                "condition": "above",
                "threshold": resistance,
                "expires_at": None,
                "pinned": False,
            })

        alert_above = _as_float(vals.get("alert_above"))
        if alert_above is not None and alert_above > 0:
            label = f"Watch: {ticker} above {_format_threshold(alert_above)}"
            if note:
                label = f"{label} — {note}"
            _append_unique(desired, seen, {
                "source": "key_levels_watch",
                "source_key": f"watch:{ticker}:alert_above",
                "tier": 2,
                "ticker": ticker,
                "label": label,
                "condition": "above",
                "threshold": alert_above,
                "expires_at": None,
                "pinned": False,
            })

    return desired


def build_journal_alerts(alerts: list[dict], ttl_days: int = DEFAULT_JOURNAL_ALERT_TTL_DAYS) -> list[dict]:
    """Compile journal-derived alerts with a fixed source key per ticker/condition."""
    desired: list[dict] = []
    seen: set[tuple[str, str]] = set()
    expires_at = _utcnow() + dt.timedelta(days=ttl_days)

    for alert in alerts or []:
        ticker = str(alert.get("ticker") or "").strip().upper()
        condition = str(alert.get("condition") or "").strip().lower()
        threshold = _as_float(alert.get("threshold"))
        if not ticker or condition not in {"above", "below"} or threshold is None:
            continue

        label = str(alert.get("label") or "").strip()
        if not label:
            label = f"Journal: {ticker} {condition} {_format_threshold(threshold)}"

        _append_unique(desired, seen, {
            "source": "journal_sync",
            "source_key": f"journal:{ticker}:{condition}",
            "tier": 3,
            "ticker": ticker,
            "label": label,
            "condition": condition,
            "threshold": threshold,
            "expires_at": expires_at,
            "pinned": False,
        })

    return desired


def _load_managed_alerts(cur, sources: tuple[str, ...]) -> list[dict]:
    cur.execute(
        """
        SELECT id, source, source_key, ticker, label, condition, threshold::float,
               enabled, triggered, tier, expires_at, archived_at, pinned
        FROM price_alerts
        WHERE source = ANY(%s)
        ORDER BY source, source_key, id DESC
        """,
        (list(sources),),
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _needs_reactivation(existing: dict) -> bool:
    return not existing.get("enabled", True) or existing.get("archived_at") is not None


def _unchanged(existing: dict, desired: dict) -> bool:
    if _needs_reactivation(existing):
        return False
    existing_threshold = _as_float(existing.get("threshold"))
    desired_threshold = _as_float(desired.get("threshold"))
    if existing_threshold is None or desired_threshold is None:
        return False
    return (
        abs(existing_threshold - desired_threshold) < 1e-9
        and (existing.get("ticker") or "") == (desired.get("ticker") or "")
        and (existing.get("label") or None) == (desired.get("label") or None)
        and existing.get("condition") == desired.get("condition")
        and int(existing.get("tier") or 0) == int(desired.get("tier") or 0)
        and existing.get("expires_at") == desired.get("expires_at")
        and bool(existing.get("pinned")) == bool(desired.get("pinned"))
    )


def _apply_upsert(cur, existing: dict | None, desired: dict) -> str:
    """Insert or update a managed alert. Returns action label."""
    now = _utcnow()
    source = desired["source"]
    source_key = desired["source_key"]
    ticker = desired["ticker"]
    label = desired["label"]
    condition = desired["condition"]
    threshold = desired["threshold"]
    tier = desired.get("tier", 2)
    expires_at = desired.get("expires_at")
    pinned = bool(desired.get("pinned", False))

    if existing is None:
        cur.execute(
            """
            INSERT INTO price_alerts
                (ticker, label, condition, threshold, source, source_key, tier,
                 expires_at, pinned, refreshed_at, enabled, archived_at, triggered)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, TRUE, NULL, FALSE)
            """,
            (ticker, label or None, condition, threshold, source, source_key, tier,
             expires_at, pinned, now),
        )
        return "created"

    if _unchanged(existing, desired):
        return "unchanged"

    params = [
        ticker,
        label or None,
        condition,
        threshold,
        source,
        source_key,
        tier,
        expires_at,
        pinned,
        now,
    ]
    sql = """
        UPDATE price_alerts
        SET ticker = %s,
            label = %s,
            condition = %s,
            threshold = %s,
            source = %s,
            source_key = %s,
            tier = %s,
            expires_at = %s,
            pinned = %s,
            refreshed_at = %s,
            enabled = TRUE,
            archived_at = NULL
    """
    if _needs_reactivation(existing):
        sql += ", triggered = FALSE"
    sql += " WHERE id = %s"
    params.append(existing["id"])
    cur.execute(sql, params)
    return "updated"


def _archive_alert(cur, alert_id: int) -> None:
    now = _utcnow()
    cur.execute(
        """
        UPDATE price_alerts
        SET enabled = FALSE,
            triggered = FALSE,
            archived_at = %s,
            refreshed_at = %s
        WHERE id = %s
        """,
        (now, now, alert_id),
    )


def prune_expired_alerts(*, dry_run: bool = False) -> int:
    """Archive any enabled alerts whose expires_at has passed."""
    now = _utcnow()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE price_alerts
                SET enabled = FALSE,
                    triggered = FALSE,
                    archived_at = %s,
                    refreshed_at = %s
                WHERE enabled = TRUE
                  AND expires_at IS NOT NULL
                  AND expires_at <= %s
                """,
                (now, now, now),
            )
            archived = cur.rowcount or 0

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return archived
    finally:
        conn.close()


def backfill_legacy_structural_alerts(*, dry_run: bool = False) -> int:
    """Convert legacy P10 watch alerts into managed structural rows."""
    now = _utcnow()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ticker, label, condition, threshold::float
                FROM price_alerts
                WHERE (source IS NULL OR source = 'manual')
                  AND source_key IS NULL
                  AND label LIKE 'Watch: %%'
                ORDER BY id
                """
            )
            rows = cur.fetchall()
            if not rows:
                if dry_run:
                    conn.rollback()
                return 0

            cols = [d.name for d in cur.description]
            count = 0
            for row in rows:
                alert = dict(zip(cols, row))
                source_key = f"watch:{alert['ticker']}:alert_above"
                cur.execute(
                    """
                    UPDATE price_alerts
                    SET source = 'key_levels_watch',
                        source_key = %s,
                        tier = 2,
                        pinned = FALSE,
                        archived_at = NULL,
                        refreshed_at = %s
                    WHERE id = %s
                    """,
                    (source_key, now, alert["id"]),
                )
                count += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return count
    finally:
        conn.close()


def reconcile_alerts(
    desired_alerts: list[dict],
    *,
    dry_run: bool = False,
    archive_missing: bool = True,
    sources: tuple[str, ...] = MANAGED_SOURCES,
) -> dict:
    """Reconcile managed alerts against a desired source-driven set.

    Only rows whose source is in `sources` are loaded/touched — this keeps a
    structural-only reconcile from treating journal_sync rows (or vice versa)
    as "missing" and archiving them.
    """
    desired_map: dict[tuple[str, str], dict] = {}
    for alert in desired_alerts or []:
        source = str(alert.get("source") or "").strip()
        source_key = str(alert.get("source_key") or "").strip()
        if not source or not source_key:
            continue
        desired_map[(source, source_key)] = dict(alert)

    stats = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "archived": 0,
        "deduped": 0,
        "desired": len(desired_map),
    }

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            current_rows = _load_managed_alerts(cur, sources)
            groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
            for row in current_rows:
                source = row.get("source")
                source_key = row.get("source_key")
                if not source or not source_key:
                    continue
                groups[(source, source_key)].append(row)

            for key, rows in groups.items():
                keeper = rows[0]
                extras = rows[1:]
                desired = desired_map.get(key)

                if extras:
                    for extra in extras:
                        _archive_alert(cur, extra["id"])
                    stats["deduped"] += len(extras)

                if desired is None:
                    if not archive_missing:
                        continue
                    _archive_alert(cur, keeper["id"])
                    stats["archived"] += 1
                    continue

                action = _apply_upsert(cur, keeper, desired)
                stats[action] += 1

            for key, desired in desired_map.items():
                if key in groups:
                    continue
                action = _apply_upsert(cur, None, desired)
                stats[action] += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return stats
    finally:
        conn.close()


def reconcile_structural_alerts(key_levels: dict, *, dry_run: bool = False) -> dict:
    """Reconcile structural alerts from key_levels."""
    backfilled = backfill_legacy_structural_alerts(dry_run=dry_run)
    stats = reconcile_alerts(
        build_structural_alerts(key_levels), dry_run=dry_run, sources=("key_levels_watch",)
    )
    stats["backfilled"] = backfilled
    return stats


def reconcile_journal_alerts(alerts: list[dict], *, dry_run: bool = False) -> dict:
    """Reconcile journal-derived alerts."""
    return reconcile_alerts(
        build_journal_alerts(alerts), dry_run=dry_run, archive_missing=False, sources=("journal_sync",)
    )


def refresh_structural_alerts_from_db(*, dry_run: bool = False) -> dict:
    """Load key levels from the DB and reconcile structural alerts."""
    from morning_brief.fetchers import load_key_levels_from_db

    return reconcile_structural_alerts(load_key_levels_from_db(), dry_run=dry_run)


def reclassify(
    old_source: str,
    old_key: str,
    new_source: str,
    new_key: str,
    *,
    label: str | None = None,
    condition: str | None = None,
    threshold: float | int | None = None,
    tier: int | None = None,
    expires_at: dt.datetime | None = None,
    pinned: bool | None = None,
    dry_run: bool = False,
) -> bool:
    """Move one managed alert across source boundaries."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ticker, label, condition, threshold::float, tier,
                       expires_at, pinned, enabled, archived_at
                FROM price_alerts
                WHERE source = %s AND source_key = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (old_source, old_key),
            )
            row = cur.fetchone()
            if row is None:
                if dry_run:
                    conn.rollback()
                return False

            cols = [d.name for d in cur.description]
            existing = dict(zip(cols, row))
            now = _utcnow()
            desired = {
                "source": new_source,
                "source_key": new_key,
                "ticker": existing["ticker"],
                "label": label if label is not None else existing["label"],
                "condition": condition if condition is not None else existing["condition"],
                "threshold": _as_float(threshold) if threshold is not None else existing["threshold"],
                "tier": tier if tier is not None else existing["tier"],
                "expires_at": expires_at if expires_at is not None else existing["expires_at"],
                "pinned": pinned if pinned is not None else existing["pinned"],
            }

            cur.execute(
                """
                UPDATE price_alerts
                SET source = %s,
                    source_key = %s,
                    ticker = %s,
                    label = %s,
                    condition = %s,
                    threshold = %s,
                    tier = %s,
                    expires_at = %s,
                    pinned = %s,
                    refreshed_at = %s,
                    enabled = TRUE,
                    archived_at = NULL,
                    triggered = FALSE
                WHERE id = %s
                """,
                (
                    desired["source"],
                    desired["source_key"],
                    desired["ticker"],
                    desired["label"],
                    desired["condition"],
                    desired["threshold"],
                    desired["tier"],
                    desired["expires_at"],
                    bool(desired["pinned"]),
                    now,
                    existing["id"],
                ),
            )

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return True
    finally:
        conn.close()
