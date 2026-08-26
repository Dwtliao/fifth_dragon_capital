---
name: trading-diary-location
description: Know where David's trading journal and morning brief files actually live — his Dropbox-synced trading_diary folder, not any cloud-sandbox path — and the exact filename convention this codebase's journal_sync.py depends on. Use whenever writing, saving, or delivering a "trading journal," "daily journal," or "trading diary" entry for David.
---

# Trading Diary Location

## The rule

`$TRADING_DIARY` (see `morning_brief/brief.py`, `morning_brief/journal_sync.py`) defaults to:

**`~/Library/CloudStorage/Dropbox/Etrade/trading_diary/`**
(full path on his Home Mac Mini: `/Users/davidliao/Library/CloudStorage/Dropbox/Etrade/trading_diary/`)

It can be overridden via the `TRADING_DIARY` env var or `.env` — check there first if anything seems off, but this is the default and the folder actually in use as of the last read.

This is a Dropbox-synced folder on David's machine, reached (from a cloud session) through the `mcp__remote-devices__*` device-bridge tools — never treat a cloud-sandbox path like `/home/claude/journal_....md` as the real destination; that only exists in a throwaway session workspace.

Do not confuse this with `trading_diary/briefs/` (dated archive copies of the morning brief, written automatically by `brief.py`) or `trading_diary/morning_brief.md` (the latest brief, also automatic) — daily trading journal entries go directly in the `trading_diary/` root.

## Filename convention (required — `journal_sync.py` globs on this exact pattern)

`trading_journal_<month><day>_<year>.md`

- `<month>` — full month name, lowercase (`july`, `august`, `june`, …)
- `<day>` — day of month, **no leading zero**
- `<year>` — 4-digit year

Example: August 11, 2026 → `trading_journal_august11_2026.md`.

`morning_brief/journal_sync.py` runs `diary.glob("trading_journal_*.md")` — a file that doesn't match this pattern is silently invisible to the sync pipeline (see the `journal-sync-workflow` skill). This is not cosmetic; getting the filename wrong means the entry never becomes a stop/watch-level/alert.

## Steps to save a journal entry

1. Write the journal content (often in combination with `fifth-dragon-eod-journal` for dashboard-sourced EOD entries).
2. Save it to a file in the working session with the correct filename.
3. Load device-bridge tools if deferred: `ToolSearch` with `select:mcp__remote-devices__get_device_info,mcp__remote-devices__device_list_dir,mcp__remote-devices__device_stage_files,mcp__remote-devices__device_commit_files,mcp__remote-devices__device_request_folder_access`.
4. Request folder access to `~/Dropbox/Etrade/trading_diary` if not already connected this session.
5. Deliver via `SendUserFile` (so David has it in-conversation regardless) **and** write it to the real location with `device_commit_files`, targeting `.../trading_diary/trading_journal_<month><day>_<year>.md`.
6. Confirm in plain language that it was saved to the real Dropbox folder with the exact filename — don't just say "delivered."
7. Point David (or offer) to the `journal-sync-workflow` skill's manual sync step — saving the file alone does not feed it into the next morning brief.

## Notes / Gotchas

- `device_commit_files` refuses to overwrite if the device file's mtime has drifted since last staged — don't blindly pass `force: true` if David may have hand-edited that day's entry.
- The device bridge only works while David's desktop app is connected — if it's not, tell him the file is ready and you'll push it once he's connected, rather than silently giving up.
- Don't touch `morning_brief.md`, `briefs/`, or `SESSION_CONTEXT.md` in that folder — this skill only concerns new daily journal entries.
