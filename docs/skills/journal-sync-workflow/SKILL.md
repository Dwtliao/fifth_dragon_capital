---
name: journal-sync-workflow
description: Understand how David's trading journal entries turn into the automated Morning Brief — what morning_brief/journal_sync.py extracts, where it writes, and that it is a MANUAL step David runs himself (not on a schedule). Use whenever discussing the morning brief pipeline, journal_sync, key_levels, price_alerts, or when David asks "will this journal feed into tomorrow's brief."
---

# Journal → Morning Brief Sync Workflow

## The pipeline, end to end

1. David (or Claude, via `fifth-dragon-eod-journal` + `trading-diary-location`) writes a dated entry to `trading_diary/trading_journal_<month><day>_<year>.md`.
2. David **manually** runs `python -m morning_brief.journal_sync` (all new/modified journals) or `--file <path>` (one file), typically from the P10 Morning Brief dashboard sidebar ("🔄 Sync Latest Journal" / "🔄 Sync All Journals"), sometimes from the CLI directly.
3. `journal_sync.py` sends the raw journal text to the Anthropic API (`claude-sonnet-4-6`) with a prompt that extracts only **actionable** levels — not historical observations — into three buckets:
   - `positions` → stop/note per ticker, merged into `key_levels` (DB-backed, via `load_key_levels_from_db`/`save_key_levels_to_db`)
   - `watch_levels` → support/resistance/alert_above per ticker, merged into `key_levels.watch`
   - `price_alerts` → conditional triggers (e.g. "if NQ above 30200"), ticker-normalized to yfinance format (NQ→NQ=F, Gold→GC=F, VIX→^VIX, etc.), reconciled into the `price_alerts` DB table via `alert_compiler.reconcile_journal_alerts` (dedup, create/update, expire pruning via `prune_expired_alerts`).
4. Every processed file is logged in `journal_sync_log` (path, mtime, counts, raw extraction JSON) — re-running skips already-synced files unless `--all` (force) is passed. `--dry-run` prints the extraction without writing to the DB.
5. **Separately, on a real schedule** (launchd, 6:45am daily, after the 6:00am `sync_daily.sh` position sync): `python -m morning_brief.brief` reads `key_levels` back out of the DB (already reflecting anything journal_sync merged in), pulls fresh market data, and writes `trading_diary/morning_brief.md` (+ dated archive copy in `trading_diary/briefs/`). This is what P10's Tab 1 renders.

## The one fact to never assume away

**Writing/saving a journal entry does NOT automatically feed the brief.** `journal_sync.py` is not on a launchd schedule — only `brief.py` (6:45am) and `sync_daily.sh` (6:00am) are. David runs the sync manually. If Claude writes a journal entry for him, it should say so explicitly — e.g. "saved to trading_diary — run journal_sync (or the P10 sidebar button) when you want this reflected in tomorrow's brief" — rather than implying it's already wired in.

## Key files or the model needs

- `morning_brief/journal_sync.py` — the extractor/sync script (see docstring at top for CLI flags: `--file`, `--dry-run`, `--all`).
- `morning_brief/alert_compiler.py` — `reconcile_journal_alerts`, `reconcile_structural_alerts`, `prune_expired_alerts`.
- `morning_brief/fetchers.py` — `load_key_levels_from_db`/`save_key_levels_to_db`.
- `morning_brief/brief.py` — the 6:45am entrypoint; `_diary_path()` resolves `$TRADING_DIARY` (see `trading-diary-location` skill).
- Root `CLAUDE.md` — authoritative project context; re-read it if anything here seems stale, and note its own "Last updated" date.

## Notes / Gotchas

- The extraction is genuinely an LLM call (not regex) — it can miss or misjudge "actionable" vs. "historical," so treat `--dry-run` output as worth a glance if David wants confidence before it writes to the DB, especially on unusually long/dense entries.
- `sync-daily` (6am) is *expected* to fail every morning per `CLAUDE.md`'s "Known open items" — E*TRADE tokens expire at midnight ET — this is unrelated to journal_sync and not something to flag as broken.
- If `CLAUDE.md`'s "Known open items" section describes a different sync/brief behavior than this skill, defer to `CLAUDE.md` — it's the project's own source of truth and gets updated as the code changes; this skill can drift.
