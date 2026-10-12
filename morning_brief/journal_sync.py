"""
journal_sync.py — Extract actionable levels from trading journals using Claude API.

Extraction stages context, watch ideas, alert requests and stop proposals for P10 review.
No live key levels or alert configuration is changed until explicit operator approval.

Usage:
    python -m morning_brief.journal_sync              # check last 5 modified journals
    python -m morning_brief.journal_sync --file PATH  # process one specific file
    python -m morning_brief.journal_sync --dry-run    # print extraction, do not write to DB
    python -m morning_brief.journal_sync --force      # reprocess selected files only
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

from etrade_sync.db import get_connection
from morning_brief.journal_review import VERSION, stage_extraction, normalize_extraction, ensure_review_ready
from morning_brief.llm import call_claude, print_usage

# ── config ────────────────────────────────────────────────────────────────────

DEFAULT_DIARY = Path.home() / "Library/CloudStorage/Dropbox/Etrade/trading_diary"

def _diary_path() -> Path:
    return Path(os.getenv("TRADING_DIARY", str(DEFAULT_DIARY)))

EXTRACTION_PROMPT = """You are parsing a personal trading journal to extract ACTIONABLE trading levels.

Return ONLY a JSON object with these three required arrays and optional commentary array.
Do not include any text outside the JSON.

Rules:
- Only extract levels the trader PLANS TO ACT ON — stops to honor, levels to watch for entries/exits, conditional triggers.
- Do NOT extract historical observation prices (e.g. "gold hit $4,400 intraday" is not actionable).
- Extraction NEVER authorizes live alerts or changes to stops; it stages review ideas.
- Every item MUST include evidence: an exact verbatim excerpt containing instrument and level.
- Every numeric item MUST include timeframe: price_crossing, daily_close, weekly_close,
  sustained, range, or unspecified. Preserve timeframe/range meaning, never silently simplify.
- Mentions/scenarios and headings "Alerts" or "HIGH" are watch ideas, not notification requests.
  price_alerts is ONLY for personal instructions like "alert me", "notify me", "set an alert".
- positions contains proposed stops, watch_levels explicit reference/entry levels. Never
  substitute futures/ETF prices for index prices. Optional commentary array holds context only.
- commentary items must be OBJECTS with ticker, note, and evidence (exact journal excerpt),
  never plain strings. Use [] or omit commentary when no ticker-specific context exists.
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
    {"ticker": "URNM", "stop": 54.95, "note": "hold", "evidence": "URNM stop 54.95", "timeframe": "price_crossing"}
  ],
  "watch_levels": [
    {"ticker": "GC=F", "support": 4300, "resistance": 4489, "alert_above": null, "evidence": "Gold support 4300 and resistance 4489", "timeframe": "unspecified"}
  ],
  "price_alerts": [
    {"ticker": "GC=F", "condition": "below", "threshold": 4200, "evidence": "Alert me if gold falls below 4200", "timeframe": "price_crossing"}
  ],
  "commentary": [
    {"ticker": "SMH", "note": "Leadership context only", "evidence": "SMH leadership looks weak."}
  ]
}

All numeric fields (stop, support, resistance, alert_above, threshold) must be numbers or null — never strings.
Only include positions/watch_levels/alerts you actually found — empty arrays are fine.

Journal text:
"""


# ── Claude extraction ─────────────────────────────────────────────────────────

def extract_from_journal(text: str, *, model=None, effort=None) -> dict:
    """Call Claude API and return parsed extraction dict."""
    result = call_claude(EXTRACTION_PROMPT + text, "journal", model=model, effort=effort)
    print_usage(result)
    raw = result["text"]
    # Strip markdown code fence if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    extracted = json.loads(raw)
    if not isinstance(extracted, dict) or any(
        not isinstance(extracted.get(key), list) for key in ("positions", "watch_levels", "price_alerts")
    ):
        raise ValueError("Claude extraction must contain positions, watch_levels and price_alerts arrays")
    return extracted


# ── DB helpers ────────────────────────────────────────────────────────────────

def _already_synced(file_path: str, mtime: float) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_mtime,extraction_version FROM journal_sync_log WHERE file_path = %s",
                (file_path,)
            )
            row = cur.fetchone()
            return row is not None and row[1] == VERSION and abs(row[0] - mtime) < 1
    finally:
        conn.close()


def _log_sync(file_path: str, mtime: float, counts: dict, dry_run: bool, extracted: dict = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO journal_sync_log
                    (file_path, file_mtime, positions_updated, watch_updated, alerts_created, dry_run, extracted_json,extraction_version,proposal_counts)
                VALUES (%s, %s, %s, %s, %s, %s, %s,%s,%s::jsonb)
                ON CONFLICT (file_path) DO UPDATE SET
                    file_mtime        = EXCLUDED.file_mtime,
                    synced_at         = NOW(),
                    positions_updated = EXCLUDED.positions_updated,
                    watch_updated     = EXCLUDED.watch_updated,
                    alerts_created    = EXCLUDED.alerts_created,
                    dry_run           = EXCLUDED.dry_run,
                    extracted_json    = EXCLUDED.extracted_json,
                    extraction_version = EXCLUDED.extraction_version,
                    proposal_counts = EXCLUDED.proposal_counts
            """, (file_path, mtime,
                  counts.get("positions", 0),
                  counts.get("watch", 0),
                  counts.get("alerts", 0),
                  dry_run,
                  json.dumps(extracted) if extracted else None, VERSION, json.dumps(counts)))
        conn.commit()
    finally:
        conn.close()


# ── apply extraction to DB ────────────────────────────────────────────────────

def apply_extraction(extracted: dict, dry_run: bool = False, *, text='', file_path='') -> dict:
    """Stage review only; never write live key levels or alert configuration."""
    if dry_run:
        return dict(positions=0,watch=0,alerts=0,extracted=len(normalize_extraction(extracted,text,file_path)))
    return stage_extraction(extracted,text,file_path)


# ── main ──────────────────────────────────────────────────────────────────────

def process_file(path: Path, dry_run: bool = False, force: bool = False, *, model=None, effort=None) -> dict:
    mtime = path.stat().st_mtime
    if not dry_run:
        ensure_review_ready()
    if not (force or dry_run) and _already_synced(str(path), mtime):
        print(f"  skip (already synced): {path.name}")
        return {}

    print(f"\n── {path.name} ──")
    text = path.read_text(encoding="utf-8")

    print("  calling Claude API…")
    extracted = extract_from_journal(text, model=model, effort=effort)

    if dry_run:
        print("  extraction (dry-run):")
        print(json.dumps(extracted, indent=2))
        # Model evaluation must not require DB reads or modify sync history.
        return dict(positions=0,watch=0,alerts=0,extracted=len(normalize_extraction(extracted,text,path)))

    counts = apply_extraction(extracted, dry_run=dry_run, text=text, file_path=path)

    if not dry_run:
        _log_sync(str(path), mtime, counts, dry_run=False, extracted=extracted)
        print(f"  done: {counts['extracted']} ideas extracted; {counts['pending']} pending; "
              f"{counts['ideas']} watch ideas; {counts['blocked']} blocked; "
              f"{counts['repeated']} repeated; 0 alerts activated, 0 replacements. Review in P10.")

    return counts


def recent_journals(diary, count=5):
    if count not in range(1,6):
        raise ValueError('Choose between 1 and 5 journal files')
    files = sorted(diary.glob('trading_journal_*.md'), key=lambda path:(path.stat().st_mtime,path.name))
    return files[-count:]


def main():
    parser = argparse.ArgumentParser(description="Stage recent trading journals for approval (maximum 5 files)")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--file", help="Process one specific journal file")
    selection.add_argument("--recent", type=int, choices=range(1,6), default=None,
                           help="Check the last 1–5 modified journals (default 5)")
    parser.add_argument("--dry-run", action="store_true", help="Print extraction, do not write to DB")
    parser.add_argument("--force", action="store_true", help="Ignore sync history for selected files only")
    parser.add_argument("--model", help="Override CLAUDE_MODEL for this run")
    parser.add_argument("--effort", choices=["low", "medium", "high"], help="Override journal effort for this run")
    args = parser.parse_args()

    if args.file:
        files = [Path(args.file)]
    else:
        diary = _diary_path()
        files = recent_journals(diary, args.recent or 5)
        if not files:
            print(f"No journal files found in {diary}")
            return

    print(f"Checking {len(files)} selected journal file(s); unchanged files are skipped. Selection is by modification time, not five calendar days.")

    total = {"extracted": 0, "pending": 0, "ideas": 0, "blocked": 0, "repeated": 0}
    for f in files:
        counts = process_file(f, dry_run=args.dry_run, force=args.force, model=args.model, effort=args.effort)
        for k in total:
            total[k] += counts.get(k, 0)

    print(f"\nTotal review counts: {total}; 0 live levels changed; 0 alerts activated. Open P10 Journal Review.")


if __name__ == "__main__":
    main()
