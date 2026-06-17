"""
journal_sync.py — Extract actionable levels from trading journals using Claude API.

Three extraction targets per journal:
  1. Portfolio table  → key_levels positions (stops + notes)
  2. Action Plan      → key_levels watch levels (support / resistance)
  3. Conditional text → price_alerts (e.g. "if NQ above 30200")

Usage:
    python -m morning_brief.journal_sync              # process all new/modified journals
    python -m morning_brief.journal_sync --file PATH  # process one specific file
    python -m morning_brief.journal_sync --dry-run    # print extraction, do not write to DB
    python -m morning_brief.journal_sync --all        # reprocess all journals (ignore sync log)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import anthropic

from etrade_sync.db import get_connection
from morning_brief.fetchers import load_key_levels_from_db, save_key_levels_to_db

# ── config ────────────────────────────────────────────────────────────────────

DEFAULT_DIARY = Path.home() / "Library/CloudStorage/Dropbox/Etrade/trading_diary"

def _diary_path() -> Path:
    return Path(os.getenv("TRADING_DIARY", str(DEFAULT_DIARY)))

CLAUDE_MODEL = "claude-sonnet-4-6"

EXTRACTION_PROMPT = """You are parsing a personal trading journal to extract ACTIONABLE trading levels.

Return ONLY a JSON object with these three keys. Do not include any other text.

Rules:
- Only extract levels the trader PLANS TO ACT ON — stops to honor, levels to watch for entries/exits, conditional triggers.
- Do NOT extract historical observation prices (e.g. "gold hit $4,400 intraday" is not actionable).
- Map ticker names to yfinance format:
    NQ, Nasdaq futures → NQ=F
    ES, S&P futures → ES=F
    Gold, GC → GC=F
    Silver, SI → SI=F
    Oil, Crude, WTI → CL=F
    VIX → ^VIX
    DXY, Dollar Index → DX-Y.NYB
    Standard equity tickers (URNM, UEC, VIXY, FNV, etc.) → unchanged

JSON schema:
{
  "positions": [
    {"ticker": "URNM", "stop": 54.95, "note": "uranium ETF — hold"}
  ],
  "watch_levels": [
    {"ticker": "GC=F", "support": 4300, "resistance": 4489, "alert_above": null, "note": "floor going into weekend"}
  ],
  "price_alerts": [
    {"ticker": "NQ=F", "condition": "above", "threshold": 30200, "label": "VIXY rebuy consideration"},
    {"ticker": "GC=F", "condition": "below", "threshold": 4200, "label": "Gold re-entry zone"}
  ]
}

All numeric fields (stop, support, resistance, alert_above, threshold) must be numbers or null — never strings.
Only include positions/watch_levels/alerts you actually found — empty arrays are fine.

Journal text:
"""


# ── Claude extraction ─────────────────────────────────────────────────────────

def extract_from_journal(text: str) -> dict:
    """Call Claude API and return parsed extraction dict."""
    client = anthropic.Anthropic()
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": EXTRACTION_PROMPT + text}],
    )
    raw = message.content[0].text.strip()
    # Strip markdown code fence if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _already_synced(file_path: str, mtime: float) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_mtime FROM journal_sync_log WHERE file_path = %s",
                (file_path,)
            )
            row = cur.fetchone()
            return row is not None and abs(row[0] - mtime) < 1
    finally:
        conn.close()


def _log_sync(file_path: str, mtime: float, counts: dict, dry_run: bool, extracted: dict = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO journal_sync_log
                    (file_path, file_mtime, positions_updated, watch_updated, alerts_created, dry_run, extracted_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (file_path) DO UPDATE SET
                    file_mtime        = EXCLUDED.file_mtime,
                    synced_at         = NOW(),
                    positions_updated = EXCLUDED.positions_updated,
                    watch_updated     = EXCLUDED.watch_updated,
                    alerts_created    = EXCLUDED.alerts_created,
                    dry_run           = EXCLUDED.dry_run,
                    extracted_json    = EXCLUDED.extracted_json
            """, (file_path, mtime,
                  counts.get("positions", 0),
                  counts.get("watch", 0),
                  counts.get("alerts", 0),
                  dry_run,
                  json.dumps(extracted) if extracted else None))
        conn.commit()
    finally:
        conn.close()


def _upsert_alert(ticker: str, condition: str, threshold: float, label: str) -> bool:
    """Insert alert if no identical one exists. Returns True if inserted."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM price_alerts
                WHERE ticker = %s AND condition = %s AND threshold = %s
            """, (ticker, condition, threshold))
            if cur.fetchone():
                return False
            cur.execute("""
                INSERT INTO price_alerts (ticker, label, condition, threshold)
                VALUES (%s, %s, %s, %s)
            """, (ticker, label or None, condition, threshold))
        conn.commit()
        return True
    finally:
        conn.close()


# ── apply extraction to DB ────────────────────────────────────────────────────

def apply_extraction(extracted: dict, dry_run: bool = False) -> dict:
    """Merge extracted data into key_levels DB and price_alerts. Returns counts."""
    counts = {"positions": 0, "watch": 0, "alerts": 0}

    kl = load_key_levels_from_db()

    # ── positions ──────────────────────────────────────────────────────────────
    for pos in extracted.get("positions") or []:
        ticker = str(pos.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        entry = kl["positions"].get(ticker, {})
        if pos.get("stop") is not None:
            entry["stop"] = float(pos["stop"])
        if pos.get("note"):
            entry["note"] = pos["note"]
        kl["positions"][ticker] = entry
        counts["positions"] += 1
        print(f"  position: {ticker}  stop={pos.get('stop')}  note={pos.get('note')}")

    # ── watch levels ───────────────────────────────────────────────────────────
    for w in extracted.get("watch_levels") or []:
        ticker = str(w.get("ticker", "")).strip()
        if not ticker:
            continue
        entry = kl["watch"].get(ticker, {})
        for field in ("support", "resistance", "alert_above"):
            if w.get(field) is not None:
                entry[field] = float(w[field])
        if w.get("note"):
            entry["note"] = w["note"]
        kl["watch"][ticker] = entry
        counts["watch"] += 1
        print(f"  watch: {ticker}  support={w.get('support')}  resistance={w.get('resistance')}  alert_above={w.get('alert_above')}")

    if not dry_run and (counts["positions"] or counts["watch"]):
        save_key_levels_to_db(kl)

    # ── price alerts ───────────────────────────────────────────────────────────
    for alert in extracted.get("price_alerts") or []:
        ticker    = str(alert.get("ticker", "")).strip()
        condition = str(alert.get("condition", "")).strip().lower()
        threshold = alert.get("threshold")
        label     = alert.get("label", "")
        if not ticker or condition not in ("above", "below") or threshold is None:
            continue
        print(f"  alert: {ticker} {condition} {threshold}  [{label}]")
        if not dry_run:
            inserted = _upsert_alert(ticker, condition, float(threshold), label)
            if inserted:
                counts["alerts"] += 1

    return counts


# ── main ──────────────────────────────────────────────────────────────────────

def process_file(path: Path, dry_run: bool = False, force: bool = False) -> dict:
    mtime = path.stat().st_mtime
    if not force and _already_synced(str(path), mtime):
        print(f"  skip (already synced): {path.name}")
        return {}

    print(f"\n── {path.name} ──")
    text = path.read_text(encoding="utf-8")

    print("  calling Claude API…")
    extracted = extract_from_journal(text)

    if dry_run:
        print("  extraction (dry-run):")
        print(json.dumps(extracted, indent=2))

    counts = apply_extraction(extracted, dry_run=dry_run)

    if not dry_run:
        _log_sync(str(path), mtime, counts, dry_run=False, extracted=extracted)
        print(f"  done: {counts['positions']} positions, {counts['watch']} watch levels, {counts['alerts']} alerts")

    return counts


def main():
    parser = argparse.ArgumentParser(description="Sync trading journals → key_levels + price_alerts")
    parser.add_argument("--file",    help="Process a specific journal file")
    parser.add_argument("--dry-run", action="store_true", help="Print extraction, do not write to DB")
    parser.add_argument("--all",     action="store_true", help="Reprocess all journals (ignore sync log)")
    args = parser.parse_args()

    if args.file:
        files = [Path(args.file)]
    else:
        diary = _diary_path()
        files = sorted(diary.glob("trading_journal_*.md"))
        if not files:
            print(f"No journal files found in {diary}")
            return

    total = {"positions": 0, "watch": 0, "alerts": 0}
    for f in files:
        counts = process_file(f, dry_run=args.dry_run, force=args.all)
        for k in total:
            total[k] += counts.get(k, 0)

    print(f"\nTotal: {total['positions']} positions updated, {total['watch']} watch levels, {total['alerts']} alerts created")


if __name__ == "__main__":
    main()
