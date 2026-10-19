"""Alert compiler/reconciler for price_alerts.

Only configured position stops are source-managed. Saved watch levels and
journal evidence never create, refresh, reactivate, or retire discretionary alerts.
Legacy discretionary identities and operator controls remain intact.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict
from typing import Any, Callable

from alerts.lifecycle import LOCK_NAMESPACE, is_position_stop
from etrade_sync.db import get_connection

MANAGED_SOURCES = ("key_levels_watch", "journal_sync")
DEFAULT_JOURNAL_ALERT_TTL_DAYS = 21

# Near-duplicate reporting tolerance, scaled across asset prices and clamped
# for expensive futures and cheap stocks. Legacy constant names are retained.
JOURNAL_MATCH_PCT = 0.01
JOURNAL_MATCH_MIN_ABS = 0.05
JOURNAL_MATCH_MAX_ABS = 25.0

# Legacy default retained for the inert promotion compatibility entrypoint.
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


def build_structural_alerts(key_levels: dict, *, held_symbols: set[str] | None = None) -> list[dict]:
    """Compile holding stops only; saved watch levels are reference information."""
    desired: list[dict] = []
    seen: set[tuple[str, str]] = set()

    positions = dict(key_levels.get("positions") or {})

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

    return desired


def upsert_journal_alert(
    cur, ticker: str, condition: str, threshold: float, label: str,
    *, ttl_days: int = DEFAULT_JOURNAL_ALERT_TTL_DAYS,
) -> str:
    """Legacy compatibility entrypoint: journal evidence never authorizes an alert."""
    return "skipped_opt_in"


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


def _desired_map(desired_alerts: list[dict]) -> dict[tuple[str, str], dict]:
    desired_map: dict[tuple[str, str], dict] = {}
    for alert in desired_alerts or []:
        source = str(alert.get("source") or "").strip()
        source_key = str(alert.get("source_key") or "").strip()
        if not source or not source_key or not is_position_stop(alert):
            continue
        desired_map[(source, source_key)] = dict(alert)
    return desired_map


class _LockCoverageDrifted(Exception):
    """Internal control-flow signal: a reload (or a newly discovered guard
    requirement) needs a lock this attempt never acquired, or can't acquire
    now without breaking global ascending lock order. Caught by
    reconcile_alerts to abandon the attempt (releasing all its locks) and
    retry on a fresh connection, rather than acquire the missing lock out of
    order. Carries the most complete `desired_map` known at the time of the
    abandonment, so the next attempt starts already knowing about it instead
    of re-discovering — and re-abandoning over — the same gap."""

    def __init__(self, desired_map=None):
        super().__init__()
        self.desired_map = desired_map


def reconcile_alerts(
    desired_alerts: list[dict],
    *,
    dry_run: bool = False,
    archive_missing: bool = True,
    sources: tuple[str, ...] = MANAGED_SOURCES,
    reload_desired: Callable[[], list[dict]] | None = None,
) -> dict:
    """Reconcile position stops against a desired source-driven set.

    Both desired and existing discretionary rows are excluded, including on reload.

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

    # Seeds each attempt's starting desired_map. Updated on every abandonment to
    # the most complete map known at that point, so a retry starts already
    # knowing about whatever the previous attempt discovered — it never just
    # re-tries the same stale starting set and re-discovers (and re-abandons
    # over) the same gap forever.
    next_attempt_seed = desired_map

    for _attempt in range(5):
        desired_map = next_attempt_seed
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # Lock ordering must match review_action/apply_bulk's (ALL guards,
                # then ALL per-ticker symbols, then row locks) everywhere, with no
                # exceptions — including across this function's own retries. A
                # guard acquired after a symbol lock (even one taken in an earlier
                # retry attempt of this *same* call) reintroduces the deadlock the
                # ordering fix exists to prevent: another session taking the
                # reverse order can then hold what this one wants next while
                # waiting on what this one already holds. So each attempt here
                # starts from zero locks (a fresh connection) and, within that
                # attempt, acquires every guard it will ever need before it
                # acquires its first symbol lock — never the two interleaved.

                # Phase 1: stabilize and acquire ONLY guard locks, strictly in
                # ascending order. A reload can discover a guard that sorts
                # *before* one already held here — e.g. this session holds
                # guard "B" from an earlier iteration, then a reload reveals
                # guard "A" is needed too. Acquiring A now, after B, would
                # still be guard-before-symbol locally, but it breaks the
                # *global* ascending order every other session relies on:
                # another session legitimately holding A and waiting on B
                # would then deadlock against this one (holds B, waits on A).
                # So a newly discovered guard is only acquired here if it
                # sorts after everything already held; otherwise this attempt
                # is abandoned (never patched in place) and retried fresh,
                # carrying the enlarged requirement forward so the next
                # attempt acquires both in one correctly ordered pass.
                held_guards: set[str] = set()
                for _ in range(5):
                    guards, _tickers = _required_locks(cur, desired_map)
                    new_guards = sorted(guards - held_guards)
                    if held_guards and new_guards and new_guards[0] < max(held_guards):
                        raise _LockCoverageDrifted(desired_map)
                    for guard in new_guards:
                        cur.execute('SELECT pg_advisory_lock(%s,hashtext(%s))', (LOCK_NAMESPACE + 2, guard))
                    held_guards |= set(new_guards)
                    if reload_desired is None:
                        break
                    desired_map = _desired_map(reload_desired())
                    guards, _tickers = _required_locks(cur, desired_map)
                    if guards <= held_guards:
                        break
                else:
                    raise RuntimeError('Could not stabilize guard-lock coverage for this reconcile; retry later')

                # Phase 2: with guards fully covered, acquire ALL symbol locks in
                # one batch — never acquire a new guard after this point.
                _guards, tickers = _required_locks(cur, desired_map)
                for ticker in sorted(tickers):
                    symbol = int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4], 'big', signed=True)
                    cur.execute('SELECT pg_advisory_lock(%s,%s)', (LOCK_NAMESPACE + 1, symbol))
                held_tickers = set(tickers)

                if reload_desired is not None:
                    desired_map = _desired_map(reload_desired())
                    stats["desired"] = len(desired_map)
                    guards, tickers = _required_locks(cur, desired_map)
                    if not (guards <= held_guards and tickers <= held_tickers):
                        # This reload needs a guard or ticker this attempt never
                        # locked. Acquiring it now would be the exact ordering bug
                        # being fixed, so this attempt is abandoned (closing the
                        # connection below releases everything it holds) and
                        # retried from zero rather than patched in place.
                        raise _LockCoverageDrifted(desired_map)

                current_rows = _load_managed_alerts(cur, sources)
                groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
                for row in current_rows:
                    if not is_position_stop(row):
                        continue
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
        except _LockCoverageDrifted as drift:
            conn.rollback()
            if drift.desired_map is not None:
                next_attempt_seed = drift.desired_map
            continue
        finally:
            conn.close()
    raise RuntimeError('Could not stabilize lock coverage for this reconcile; retry later')


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
    backfilled = 0
    reload_desired = (
        (lambda: build_structural_alerts(reload_key_levels(), held_symbols=held))
        if reload_key_levels is not None else None
    )
    stats = reconcile_alerts(
        build_structural_alerts(key_levels, held_symbols=held), dry_run=dry_run, sources=("key_levels_watch",),
        reload_desired=reload_desired,
    )
    stats['held_archived'] = 0
    stats["backfilled"] = backfilled
    return stats


def reconcile_journal_alerts(
    alerts: list[dict], *, dry_run: bool = False, ttl_days: int = DEFAULT_JOURNAL_ALERT_TTL_DAYS,
) -> dict:
    """Legacy compatibility entrypoint: ingestion stages ideas via journal review."""
    return dict(created=0, updated=0, purged=0, skipped_structural=0, promoted=0,
                desired=0, skipped_held=0, held_archived=0, skipped_opt_in=len(alerts or []))


def promote_recurring_journal_alerts(
    *, threshold: int = PROMOTION_RECURRENCE_THRESHOLD, dry_run: bool = False,
) -> int:
    """Recurrence is evidence, never authorization to activate or rewrite an alert."""
    return 0


_KL_FIELDS = ("stop", "note")


def save_structural_key_levels(key_levels: dict, baseline_key_levels: dict, *, dry_run: bool = False) -> dict:
    """CAS-protected save of key_levels + reconcile, for UI callers (P10).

    `baseline_key_levels` is whatever the caller last loaded from the DB
    before building `key_levels` from user edits. Under the same per-ticker
    locks the compiler uses, re-reads the authoritative key_levels and
    refuses (raises ValueError) if any (section, ticker) row this call
    touches — including its `note` — differs from the baseline, in value
    or in existence, from what's currently in the DB: i.e. something else
    (e.g. a journal-review stop approval, or another session adding a row
    this call never saw) changed it since the UI loaded. Caller should
    reload and retry.

    Only rows present in `baseline_key_levels` or `key_levels` are written:
    a row in neither (added by someone else after baseline, concurrently
    with this call) is left untouched rather than wiped by a blind
    delete-and-replace of the whole table.

    `dry_run=True` performs the comparison but writes nothing and rolls
    back, same as `reconcile_structural_alerts`'s own `dry_run`.
    """
    if key_levels.get('watch'):
        raise ValueError('Saved Watch Levels are retired; create an alert in P11 instead')
    from morning_brief.fetchers import load_key_levels_from_db

    def _rows(levels):
        rows = {}
        for section in ("positions",):
            for ticker, vals in (levels.get(section) or {}).items():
                vals = vals or {}
                key = (section, ticker.strip().upper())
                rows[key] = tuple(
                    vals.get(f) if f == "note" else (float(vals[f]) if vals.get(f) is not None else None)
                    for f in _KL_FIELDS
                )
        return rows

    baseline_rows = _rows(baseline_key_levels)
    new_rows = _rows(key_levels)
    touched = set(baseline_rows) | set(new_rows)

    # Everything below shares one connection (locks + compare + save), so the
    # comparison can't read through a different, unpatched/unlocked session
    # than the one actually holding the locks.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for ticker in sorted({t for _, t in touched}):
                symbol = int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4], 'big', signed=True)
                cur.execute('SELECT pg_advisory_lock(%s,%s)', (LOCK_NAMESPACE + 1, symbol))
            cur.execute("SELECT section,ticker,stop,note FROM key_levels WHERE section='positions'")
            current_rows = {}
            for section, ticker, stop, note in cur.fetchall():
                key = (section, str(ticker).strip().upper())
                current_rows[key] = (
                    float(stop) if stop is not None else None,
                    note,
                )
            conflicts = sorted(f"{s}:{t}" for (s, t) in touched if current_rows.get((s, t)) != baseline_rows.get((s, t)))
            if conflicts:
                raise ValueError(f"Changed since this page loaded, refresh before saving: {', '.join(conflicts)}")
            for section, ticker_upper in sorted(set(baseline_rows) - set(new_rows)):
                cur.execute("DELETE FROM key_levels WHERE section=%s AND upper(ticker)=%s", (section, ticker_upper))
            for section in ("positions",):
                for ticker, vals in (key_levels.get(section) or {}).items():
                    vals = vals or {}
                    cur.execute(
                        "INSERT INTO key_levels (section,ticker,stop,note,updated_at) "
                        "VALUES (%s,%s,%s,%s,NOW()) "
                        "ON CONFLICT (section,ticker) DO UPDATE SET stop=EXCLUDED.stop,note=EXCLUDED.note,"
                        "updated_at=NOW()",
                        (section, ticker, vals.get("stop"), vals.get("note")),
                    )
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
    finally:
        conn.close()  # Releases the locks; reconcile below takes its own.

    # key_levels is now committed (or, under dry_run, rolled back) — a failure
    # from here on is NOT "nothing was saved". A CAS conflict above raises
    # before this point and is reported as a full rejection; a failure here is
    # a partial completion (levels saved, alert refresh failed) and must be
    # reported as such, not conflated with the former.
    try:
        stats = reconcile_structural_alerts(key_levels, dry_run=dry_run, reload_key_levels=load_key_levels_from_db)
    except Exception as exc:
        return {
            "created": 0, "updated": 0, "archived": 0, "deduped": 0, "backfilled": 0,
            "key_levels_saved": not dry_run,
            "error": str(exc),
        }
    stats["key_levels_saved"] = not dry_run
    return stats


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


def find_stale_alerts(*, min_age_days: int = STALE_MANUAL_ALERT_DAYS) -> list[dict]:
    """Review-only report: enabled, never-fired manual alerts older than min_age_days.
    Read-only — never modifies, archives, disables, or deletes anything.

    Structural (key_levels_watch) and journal_sync alerts are out of scope: structural
    rows retain their legacy classification. This report does not convert their
    ownership; configured expirations continue through prune_expired_alerts().

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
