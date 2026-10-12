"""Alert compiler/reconciler for price_alerts.

This module keeps source-driven alerts in sync with the price_alerts table
without touching the poller. It reconciles desired alerts from structural
sources (key levels) and journal extraction against the live table.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict
from typing import Any, Callable

from alerts.lifecycle import LOCK_NAMESPACE
from etrade_sync.db import get_connection

MANAGED_SOURCES = ("key_levels_watch", "journal_sync")
DEFAULT_JOURNAL_ALERT_TTL_DAYS = 21

# Two journal mentions of the same ticker+condition count as the same idea when
# their thresholds are within this band of each other. Percentage-based so it
# scales across asset prices, clamped so it's neither too loose on high-priced
# futures nor too tight on cheap stocks.
JOURNAL_MATCH_PCT = 0.01
JOURNAL_MATCH_MIN_ABS = 0.05
JOURNAL_MATCH_MAX_ABS = 25.0

# Recurrences (distinct sync runs matching the same journal idea) required
# before it gets promoted from a tactical journal alert into a structural one.
PROMOTION_RECURRENCE_THRESHOLD = 3

# A manual alert that's never fired and has sat this long gets flagged in the
# stale-alert report (see #63). Configurable so the cutoff can be tuned without
# changing the policy itself.
STALE_MANUAL_ALERT_DAYS = 90


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


def _held_symbols() -> set[str]:
    """Use actual broker holdings, not retained position metadata. Fail without writes."""
    from morning_brief.fetchers import fetch_positions_from_db
    rows = fetch_positions_from_db()
    if any('error' in row for row in rows):
        raise RuntimeError('Cannot apply held-symbol alert policy: holdings unavailable')
    return {str(row['symbol']).strip().upper() for row in rows
            if float(row.get('quantity') or 0) > 0}


def _archive_held_nonstop(held: set[str], *, dry_run: bool = False) -> int:
    """Retire managed watch/journal alerts, never manual or configured position stops."""
    if not held:
        return 0
    conn = get_connection()
    count = 0
    try:
        with conn.cursor() as cur:
            for row in _load_managed_alerts(cur, MANAGED_SOURCES):
                ticker = str(row['ticker']).strip().upper()
                stop = (row['source'] == 'key_levels_watch'
                        and row.get('source_key') == f"position:{row['ticker']}:stop")
                if ticker in held and not stop and row['archived_at'] is None:
                    _archive_alert(cur, row['id'])
                    count += 1
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return count
    finally:
        conn.close()


def build_structural_alerts(key_levels: dict, *, held_symbols: set[str] | None = None) -> list[dict]:
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
        if ticker.strip().upper() in (held_symbols or set()):
            continue
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
        # alert_above and resistance are the same concept (a level to watch for a
        # breakout above) — if the user set both to the same price, only emit one
        # row instead of two identical "above X" alerts for the same ticker.
        if alert_above is not None and resistance is not None and abs(alert_above - resistance) < 1e-6:
            alert_above = None
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


def build_journal_alerts(alerts: list[dict]) -> list[dict]:
    """Normalize raw journal extraction into candidate alerts.

    No dedup/keying here — matching against existing tactical journal alerts
    (same idea vs. a genuinely new one) happens per-candidate in
    `upsert_journal_alert`, since it depends on live DB state (tolerance
    against an existing row's threshold), not just this batch.
    """
    candidates: list[dict] = []

    for alert in alerts or []:
        ticker = str(alert.get("ticker") or "").strip().upper()
        condition = str(alert.get("condition") or "").strip().lower()
        threshold = _as_float(alert.get("threshold"))
        if not ticker or condition not in {"above", "below"} or threshold is None:
            continue

        label = str(alert.get("label") or "").strip()
        if not label:
            label = f"Journal: {ticker} {condition} {_format_threshold(threshold)}"

        candidates.append({
            "ticker": ticker,
            "condition": condition,
            "threshold": threshold,
            "label": label,
            "tier": 3,
        })

    return candidates


def _journal_match_tolerance(threshold: float) -> float:
    band = abs(threshold) * JOURNAL_MATCH_PCT
    return min(max(band, JOURNAL_MATCH_MIN_ABS), JOURNAL_MATCH_MAX_ABS)


def _find_matching_journal_alert(cur, ticker: str, condition: str, threshold: float) -> dict | None:
    """Return the most-recently-refreshed active, non-pinned journal alert within tolerance, if any."""
    cur.execute(
        """
        SELECT id, ticker, label, condition, threshold::float, tier, expires_at,
               pinned, enabled, archived_at, source_key, recurrence_count,
               first_seen_at, last_seen_at, refreshed_at
        FROM price_alerts
        WHERE source = 'journal_sync'
          AND pinned = FALSE
          AND enabled = TRUE
          AND archived_at IS NULL
          AND ticker = %s
          AND condition = %s
        ORDER BY refreshed_at DESC NULLS LAST, id DESC
        """,
        (ticker, condition),
    )
    cols = [d.name for d in cur.description]
    for row in cur.fetchall():
        candidate = dict(zip(cols, row))
        if abs(candidate["threshold"] - threshold) <= _journal_match_tolerance(candidate["threshold"]):
            return candidate
    return None


def _matches_existing_structural_alert(cur, ticker: str, condition: str, threshold: float) -> bool:
    """True if an active structural alert already covers this ticker/condition within tolerance."""
    cur.execute(
        """
        SELECT threshold::float
        FROM price_alerts
        WHERE source = 'key_levels_watch'
          AND enabled = TRUE
          AND archived_at IS NULL
          AND ticker = %s
          AND condition = %s
        """,
        (ticker, condition),
    )
    for (existing_threshold,) in cur.fetchall():
        if abs(existing_threshold - threshold) <= _journal_match_tolerance(existing_threshold):
            return True
    return False


def upsert_journal_alert(
    cur, ticker: str, condition: str, threshold: float, label: str,
    *, ttl_days: int = DEFAULT_JOURNAL_ALERT_TTL_DAYS,
) -> str:
    """Match against existing tactical journal alerts within tolerance; update in place or start a new idea.

    If a structural alert already covers the same ticker/condition/threshold, skip
    creating a tactical shadow of it entirely — the idea is already captured
    long-lived in key_levels_watch, so a new journal_sync row would just be noise.
    """
    now = _utcnow()
    expires_at = now + dt.timedelta(days=ttl_days)
    existing = _find_matching_journal_alert(cur, ticker, condition, threshold)

    if existing is None:
        if _matches_existing_structural_alert(cur, ticker, condition, threshold):
            return "skipped_structural"
        source_key = f"journal:{ticker}:{condition}:{now.strftime('%Y%m%dT%H%M%S%f')}"
        cur.execute(
            """
            INSERT INTO price_alerts
                (ticker, label, condition, threshold, source, source_key, tier,
                 expires_at, pinned, refreshed_at, enabled, archived_at, triggered,
                 recurrence_count, first_seen_at, last_seen_at)
            VALUES
                (%s, %s, %s, %s, 'journal_sync', %s, 3,
                 %s, FALSE, %s, TRUE, NULL, FALSE,
                 1, %s, %s)
            """,
            (ticker, label or None, condition, threshold, source_key, expires_at, now, now, now),
        )
        return "purged" if getattr(cur, "statusmessage", "") == "INSERT 0 0" else "created"

    cur.execute(
        """
        UPDATE price_alerts
        SET label = %s,
            threshold = %s,
            expires_at = %s,
            refreshed_at = %s,
            last_seen_at = %s,
            recurrence_count = recurrence_count + 1
        WHERE id = %s
        """,
        (label or None, threshold, expires_at, now, now, existing["id"]),
    )
    return "updated"


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
        return "purged" if getattr(cur, "statusmessage", "") == "INSERT 0 0" else "created"

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


def _desired_map(desired_alerts: list[dict]) -> dict[tuple[str, str], dict]:
    desired_map: dict[tuple[str, str], dict] = {}
    for alert in desired_alerts or []:
        source = str(alert.get("source") or "").strip()
        source_key = str(alert.get("source_key") or "").strip()
        if not source or not source_key:
            continue
        desired_map[(source, source_key)] = dict(alert)
    return desired_map


def reconcile_alerts(
    desired_alerts: list[dict],
    *,
    dry_run: bool = False,
    archive_missing: bool = True,
    sources: tuple[str, ...] = MANAGED_SOURCES,
    reload_desired: Callable[[], list[dict]] | None = None,
) -> dict:
    """Reconcile managed alerts against a desired source-driven set.

    Only rows whose source is in `sources` are loaded/touched — this keeps a
    structural-only reconcile from treating journal_sync rows (or vice versa)
    as "missing" and archiving them.

    `reload_desired`, if given, is called with no arguments *after* the locks
    below are held, and its result replaces `desired_alerts`. Without it, a
    caller who loaded `desired_alerts` from the DB before this function even
    started can hand it a snapshot that's gone stale by the time it is
    actually written: the locks only stop two writers from racing each other,
    they can't un-stale data read before either writer locked anything.
    """
    desired_map = _desired_map(desired_alerts)

    stats = {
        "created": 0,
        "purged": 0,
        "updated": 0,
        "unchanged": 0,
        "archived": 0,
        "deduped": 0,
        "desired": len(desired_map),
    }

    def _required_locks(cur, desired):
        # Guard keys must match review_action/apply_bulk/the DB trigger exactly
        # ("source:ticker:condition", hashtext-keyed) and target current price_alerts
        # tickers too, not just the (possibly stale) desired set.
        guards = {
            f"{source}:{str(alert.get('ticker') or '').strip().upper()}:{alert.get('condition')}"
            for (source, _), alert in desired.items()
        }
        cur.execute('SELECT DISTINCT ticker FROM price_alerts WHERE source = ANY(%s)', (list(sources),))
        tickers = {str(row[0] or '').strip().upper() for row in cur.fetchall()}
        tickers |= {str(alert.get('ticker') or '').strip().upper() for alert in desired.values()}
        return guards, {t for t in tickers if t}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Lock ordering must match review_action/apply_bulk's (guard, then
            # per-ticker symbol, then row) or the two can deadlock: a stop
            # approval takes guard-then-symbol while an insert here used to
            # take symbol-then-guard (the guard being taken implicitly, inside
            # the DB trigger, the moment an INSERT statement runs).
            held_guards: set[str] = set()
            held_tickers: set[str] = set()
            for _ in range(5):
                guards, tickers = _required_locks(cur, desired_map)
                new_guards = sorted(guards - held_guards)
                new_tickers = sorted(tickers - held_tickers)
                for guard in new_guards:
                    cur.execute('SELECT pg_advisory_lock(%s,hashtext(%s))', (LOCK_NAMESPACE + 2, guard))
                held_guards |= set(new_guards)
                for ticker in new_tickers:
                    symbol = int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4], 'big', signed=True)
                    cur.execute('SELECT pg_advisory_lock(%s,%s)', (LOCK_NAMESPACE + 1, symbol))
                held_tickers |= set(new_tickers)
                if reload_desired is None:
                    break
                desired_map = _desired_map(reload_desired())
                stats["desired"] = len(desired_map)
                # The reload can introduce a ticker/condition the first pass never
                # locked (e.g. a key level someone else just added). Locking is
                # only real protection if it covers everything we're about to read
                # and write — if the reload's requirements aren't a subset of what
                # we hold, go around again and pick up the gap before proceeding.
                guards, tickers = _required_locks(cur, desired_map)
                if guards <= held_guards and tickers <= held_tickers:
                    break
            else:
                raise RuntimeError('Could not stabilize lock coverage for this reconcile; retry later')
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


def reconcile_structural_alerts(
    key_levels: dict, *, dry_run: bool = False, reload_key_levels: Callable[[], dict] | None = None,
) -> dict:
    """Reconcile structural alerts from key_levels.

    `key_levels` is used as-is unless `reload_key_levels` is given, in which
    case it is only a placeholder used to pick the tickers to lock; the
    authoritative data reconciled against is read fresh, under those locks,
    via `reload_key_levels` (see `reconcile_alerts`'s `reload_desired`).
    """
    held = _held_symbols()
    backfilled = backfill_legacy_structural_alerts(dry_run=dry_run)
    reload_desired = (
        (lambda: build_structural_alerts(reload_key_levels(), held_symbols=held))
        if reload_key_levels is not None else None
    )
    stats = reconcile_alerts(
        build_structural_alerts(key_levels, held_symbols=held), dry_run=dry_run, sources=("key_levels_watch",),
        reload_desired=reload_desired,
    )
    stats['held_archived'] = _archive_held_nonstop(held, dry_run=dry_run)
    stats["backfilled"] = backfilled
    return stats


def reconcile_journal_alerts(
    alerts: list[dict], *, dry_run: bool = False, ttl_days: int = DEFAULT_JOURNAL_ALERT_TTL_DAYS,
) -> dict:
    """Match/refresh journal-derived alerts in place, then promote recurring ideas."""
    held = _held_symbols()
    all_candidates = build_journal_alerts(alerts)
    candidates = [a for a in all_candidates if a['ticker'].upper() not in held]
    stats = {"created": 0, "updated": 0, "purged": 0, "skipped_structural": 0, "promoted": 0, "desired": len(candidates)}
    stats['skipped_held'] = len(all_candidates) - len(candidates)
    stats['held_archived'] = _archive_held_nonstop(held, dry_run=dry_run)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for alert in candidates:
                action = upsert_journal_alert(
                    cur, alert["ticker"], alert["condition"], alert["threshold"], alert["label"],
                    ttl_days=ttl_days,
                )
                stats[action] += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()

    # Recurrence is evidence, never authorization to make an idea permanent.
    stats["promoted"] = 0
    return stats


def promote_recurring_journal_alerts(
    *, threshold: int = PROMOTION_RECURRENCE_THRESHOLD, dry_run: bool = False,
) -> int:
    """Move journal alerts that have recurred `threshold`+ times into structural (key_levels_watch) alerts."""
    held = _held_symbols()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_key, ticker, condition
                FROM price_alerts
                WHERE source = 'journal_sync'
                  AND pinned = FALSE
                  AND enabled = TRUE
                  AND archived_at IS NULL
                  AND recurrence_count >= %s
                """,
                (threshold,),
            )
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()

    promoted = 0
    for row in rows:
        if row['ticker'].strip().upper() in held:
            continue
        field = "resistance" if row["condition"] == "above" else "support"
        new_key = f"watch:{row['ticker']}:{field}"

        if _structural_row_exists(new_key):
            # key_levels.yml already has a structural belief at this ticker/field — it's
            # regenerated fresh from the YAML on every reconcile, so overwriting its
            # threshold here would just get reverted on the next brief.py/Save All run.
            # The recurring journal idea has effectively already been captured
            # structurally, so retire the tactical duplicate instead of colliding with it.
            _archive_by_source_key("journal_sync", row["source_key"], dry_run=dry_run)
            promoted += 1
            continue

        if reclassify(
            "journal_sync", row["source_key"], "key_levels_watch", new_key,
            tier=2, dry_run=dry_run,
        ):
            # reclassify() only overrides fields you pass a non-None value for, so it
            # can't be used to clear expires_at — structural alerts must be long-lived,
            # not keep carrying the journal TTL they were promoted from.
            _clear_expires_at(new_key, dry_run=dry_run)
            promoted += 1
    return promoted


def _structural_row_exists(source_key: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM price_alerts WHERE source = 'key_levels_watch' AND source_key = %s LIMIT 1",
                (source_key,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def _archive_by_source_key(source: str, source_key: str, *, dry_run: bool = False) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM price_alerts WHERE source = %s AND source_key = %s", (source, source_key))
            row = cur.fetchone()
            if row:
                _archive_alert(cur, row[0])
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()


def _clear_expires_at(source_key: str, *, dry_run: bool = False) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE price_alerts SET expires_at = NULL WHERE source = 'key_levels_watch' AND source_key = %s",
                (source_key,),
            )
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    finally:
        conn.close()


_KL_FIELDS = ("stop", "support", "resistance", "alert_above")


def save_structural_key_levels(key_levels: dict, baseline_key_levels: dict, *, dry_run: bool = False) -> dict:
    """CAS-protected save of key_levels + reconcile, for UI callers (P10).

    `baseline_key_levels` is whatever the caller last loaded from the DB
    before building `key_levels` from user edits. Under the same per-ticker
    locks the compiler uses, re-reads the authoritative key_levels and
    refuses (raises ValueError) if any ticker/field differs from the
    baseline — i.e. something else (e.g. a journal-review stop approval)
    changed it since the UI loaded, and saving now would silently clobber
    that change with stale widget state. Caller should reload and retry.
    """
    from morning_brief.fetchers import load_key_levels_from_db

    def _flat(levels):
        flat = {}
        for section in ("positions", "watch"):
            for ticker, vals in (levels.get(section) or {}).items():
                for field in _KL_FIELDS:
                    if (vals or {}).get(field) is not None:
                        flat[(ticker.strip().upper(), field)] = float(vals[field])
        return flat

    baseline_flat = _flat(baseline_key_levels)
    touched = set(baseline_flat) | set(_flat(key_levels))

    # Everything below shares one connection (locks + compare + save), so the
    # comparison can't read through a different, unpatched/unlocked session
    # than the one actually holding the locks.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for ticker in sorted({t for t, _ in touched}):
                symbol = int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4], 'big', signed=True)
                cur.execute('SELECT pg_advisory_lock(%s,%s)', (LOCK_NAMESPACE + 1, symbol))
            cur.execute("SELECT section,ticker,stop,support,resistance,alert_above FROM key_levels")
            current_flat = {}
            for section, ticker, stop, support, resistance, alert_above in cur.fetchall():
                for field, value in (("stop", stop), ("support", support), ("resistance", resistance), ("alert_above", alert_above)):
                    if value is not None:
                        current_flat[(str(ticker).strip().upper(), field)] = float(value)
            conflicts = sorted(f"{t}:{field}" for t, field in touched if current_flat.get((t, field)) != baseline_flat.get((t, field)))
            if conflicts:
                raise ValueError(f"Changed since this page loaded, refresh before saving: {', '.join(conflicts)}")
            cur.execute("DELETE FROM key_levels")
            for section in ("positions", "watch"):
                for ticker, vals in (key_levels.get(section) or {}).items():
                    cur.execute(
                        "INSERT INTO key_levels (section,ticker,stop,support,resistance,alert_above,note,updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())",
                        (section, ticker, vals.get("stop"), vals.get("support"), vals.get("resistance"),
                         vals.get("alert_above"), vals.get("note")),
                    )
        conn.commit()
    finally:
        conn.close()  # Releases the locks; reconcile below takes its own.
    return reconcile_structural_alerts(key_levels, dry_run=dry_run, reload_key_levels=load_key_levels_from_db)


def refresh_structural_alerts_from_db(*, dry_run: bool = False) -> dict:
    """Load key levels from the DB and reconcile structural alerts.

    This is the DB-authoritative entrypoint (morning pipeline, P10 "Sync
    Positions"), so it re-reads key_levels under the per-ticker locks rather
    than reconciling whatever was read before this call started — see
    `reconcile_structural_alerts`'s `reload_key_levels`.
    """
    from morning_brief.fetchers import load_key_levels_from_db

    initial = load_key_levels_from_db()
    return reconcile_structural_alerts(initial, dry_run=dry_run, reload_key_levels=load_key_levels_from_db)


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
            if getattr(cur, "statusmessage", "") == "UPDATE 0":
                conn.rollback()
                return False

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return True
    finally:
        conn.close()


def find_duplicate_alerts(
    *,
    tolerance_pct: float = JOURNAL_MATCH_PCT,
    min_abs: float = JOURNAL_MATCH_MIN_ABS,
    max_abs: float = JOURNAL_MATCH_MAX_ABS,
) -> list[dict]:
    """Review-only report: active alerts clustered by ticker+condition where thresholds
    fall within tolerance of each other, regardless of source. Read-only — never
    modifies, archives, or merges anything. Surfaces candidates for a human to review
    (e.g. cross-source duplicates like a manual alert overlapping a structural one).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ticker, source, condition, threshold::float, label
                FROM price_alerts
                WHERE enabled = TRUE AND archived_at IS NULL
                ORDER BY ticker, condition, threshold
                """
            )
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()

    def _tolerance(threshold: float) -> float:
        return min(max(abs(threshold) * tolerance_pct, min_abs), max_abs)

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["ticker"], row["condition"])].append(row)

    clusters: list[dict] = []
    for (ticker, condition), items in groups.items():
        items.sort(key=lambda r: r["threshold"])
        current = [items[0]]
        for item in items[1:]:
            if abs(item["threshold"] - current[0]["threshold"]) <= _tolerance(current[0]["threshold"]):
                current.append(item)
            else:
                if len(current) > 1:
                    clusters.append({"ticker": ticker, "condition": condition, "rows": current})
                current = [item]
        if len(current) > 1:
            clusters.append({"ticker": ticker, "condition": condition, "rows": current})

    return clusters


def print_duplicate_report() -> None:
    clusters = find_duplicate_alerts()
    if not clusters:
        print("No duplicate/near-duplicate active alert clusters found.")
        return
    for cluster in clusters:
        print(f"\n{cluster['ticker']} {cluster['condition']}:")
        for row in cluster["rows"]:
            print(f"  id={row['id']:<4} source={row['source']:<16} threshold={row['threshold']:<10} {row['label']}")


# Non-structural sources eligible for one-click consolidation against an exact
# structural match — structural is always the side that wins, per #63.
CONSOLIDATABLE_SOURCES = ("manual", "journal_sync")


def archive_duplicate_alert(candidate_id: int, structural_id: int, *, dry_run: bool = False) -> bool:
    """Narrow, deterministic consolidation: archive a manual or journal_sync alert
    that exactly duplicates a structural (key_levels_watch) alert — same ticker,
    condition, and threshold, both currently active. Structural wins because it's
    regenerated from key_levels.yml/DB on every reconcile and would just get
    reverted otherwise (same reasoning promote_recurring_journal_alerts() uses for
    its own manual/structural and journal/structural collision cases).

    Re-verifies eligibility against live data rather than trusting a caller-supplied
    cluster — returns False and archives nothing if the pair no longer qualifies
    (already archived/disabled, sources don't match, or thresholds have diverged).
    Never touches the structural row.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ticker, source, condition, threshold::float, enabled, archived_at
                FROM price_alerts
                WHERE id = ANY(%s)
                """,
                ([candidate_id, structural_id],),
            )
            cols = [d.name for d in cur.description]
            by_id = {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}

            candidate = by_id.get(candidate_id)
            structural = by_id.get(structural_id)
            qualifies = (
                candidate is not None and structural is not None
                and candidate["source"] in CONSOLIDATABLE_SOURCES and structural["source"] == "key_levels_watch"
                and candidate["enabled"] and candidate["archived_at"] is None
                and structural["enabled"] and structural["archived_at"] is None
                and candidate["ticker"] == structural["ticker"]
                and candidate["condition"] == structural["condition"]
                and candidate["threshold"] == structural["threshold"]
            )
            if not qualifies:
                conn.rollback()
                return False

            _archive_alert(cur, candidate_id)

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return True
    finally:
        conn.close()


def find_stale_alerts(*, min_age_days: int = STALE_MANUAL_ALERT_DAYS) -> list[dict]:
    """Review-only report: enabled, never-fired manual alerts older than min_age_days.
    Read-only — never modifies, archives, disables, or deletes anything.

    Structural (key_levels_watch) and journal_sync alerts are out of scope: structural
    staleness is already handled by reconcile_structural_alerts()'s archive-on-removal
    behavior, and journal alerts already have TTL pruning (prune_expired_alerts()).
    Manual alerts are the only source with no expiry mechanism at all.

    Each row is cross-checked against mv_unrealized_pnl (open positions). A ticker
    with no matching open position is the stronger signal (confidence="no_open_position") —
    though this means "no open position," not "position was closed": many manual
    alerts watch futures/macro/vol tickers (e.g. NQ=F, DX-Y.NYB, VIXY) that were never
    positions to begin with. A ticker that does have an open position falls back to a
    weaker, age-only signal (confidence="age_only"), since a live position's stop
    alert may simply be correctly dormant.
    """
    now = _utcnow()
    cutoff = now - dt.timedelta(days=min_age_days)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ticker, label, condition, threshold::float, created_at
                FROM price_alerts
                WHERE source = 'manual'
                  AND enabled = TRUE
                  AND archived_at IS NULL
                  AND last_fired_at IS NULL
                  AND created_at <= %s
                ORDER BY created_at
                """,
                (cutoff,),
            )
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

            open_symbols: set[str] = set()
            if rows:
                cur.execute(
                    "SELECT DISTINCT symbol FROM mv_unrealized_pnl WHERE quantity IS NOT NULL AND quantity != 0"
                )
                open_symbols = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    report = []
    for row in rows:
        has_open_position = row["ticker"] in open_symbols
        report.append({
            "id": row["id"],
            "ticker": row["ticker"],
            "label": row["label"],
            "condition": row["condition"],
            "threshold": row["threshold"],
            "source": "manual",
            "created_at": row["created_at"],
            "age_days": (now - row["created_at"]).days,
            "has_open_position": has_open_position,
            "confidence": "age_only" if has_open_position else "no_open_position",
        })
    return report


def print_stale_alert_report() -> None:
    rows = find_stale_alerts()
    if not rows:
        print("No stale manual alerts found.")
        return
    for row in rows:
        print(
            f"id={row['id']:<4} ticker={row['ticker']:<10} age_days={row['age_days']:<5} "
            f"confidence={row['confidence']:<17} threshold={row['threshold']:<10} {row['label']}"
        )


if __name__ == "__main__":
    import sys

    print_duplicate_report()
    if "--stale" in sys.argv:
        print_stale_alert_report()
