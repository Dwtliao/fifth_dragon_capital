---
name: fifth-dragon-eod-journal
description: Read David's Fifth Dragon Capital Streamlit dashboard (localhost:8501, reachable only via the Claude in Chrome browser extension on his Home Mac Mini) and produce an end-of-day market summary plus a dated trading journal entry saved to the real trading_diary folder. Use when David asks for an "EOD summary," "end of day," "today's journal," "trading journal highlights," or references localhost:8501 or page names like P7 Market Monitor, P8A Commodities, P2 Portfolio Overview, P6 Physical Metals, etc. Works together with the trading-diary-location skill (where/how to save the file) and the journal-sync-workflow skill (how it later feeds the Morning Brief) — read those before saving anything.
---

# Fifth Dragon Capital EOD Journal

## Context

David runs this Streamlit dashboard ("Fifth Dragon Capital," this repo — see root `CLAUDE.md`/`README.md`) locally at `http://localhost:8501`. This is NOT reachable from a cloud sandbox's own network/shell — `WebFetch`, `Bash curl`, etc. will all fail against `localhost`. It IS reachable through the **Claude in Chrome** browser extension, because that extension controls David's actual local browser, which can hit his own localhost.

Dashboard pages (`dashboard/pages/`): P1 Pipeline Status, P2 Portfolio Overview, P3 Performance, P4 Trading History, P5 Risk Exposure, P6 Physical Metals, P7 Market Monitor, P8A Commodities, P8B Technology, P9 Symbol Admin, P10 Morning Brief.

**Page-name note:** the root `CLAUDE.md` and this skill have disagreed before on whether Commodities is one page (P8) or split into P8A/P8B — `CLAUDE.md` is the project's own source of truth and gets updated as the code changes, so if the two disagree, trust `CLAUDE.md` (or just check `dashboard/pages/` directly) over this skill's page list.

David most often wants P7 and P8A read for an EOD summary, but may ask for others by name — navigate to whichever page(s) he specifies.

## Steps

1. **Load the browser tools if deferred.** ToolSearch: `select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__get_page_text,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__tabs_close_mcp,mcp__claude-in-chrome__switch_browser,mcp__claude-in-chrome__select_browser`

2. **Get tab context first**: call `tabs_context_mcp` with `createIfEmpty: true`.
   - If it errors with "Multiple Chrome browsers connected... none selected," ask via AskUserQuestion (or call `switch_browser` with no args and wait for David to click Connect). His dashboard runs on his **Home Mac Mini** browser — if he's picked that before in this session, you can suggest it, but still let him confirm/pick if multiple browsers are connected.

3. **Navigate to each requested page** with `navigate` (e.g. `http://localhost:8501/P7_Market_Monitor`, `http://localhost:8501/P8A_Commodities`). `wait` ~2s for Streamlit to render, then `get_page_text`.
   - Some multi-chart panels (e.g. a "Precious Metals Futures" grid) can render as empty `expand_more` stubs in `get_page_text` even after the wait, especially right after navigating between pages — this is a text-extraction gap, not missing data. Fall back to `screenshot` + `scroll` and read the numbers visually before concluding a section is unavailable.

4. **Read alerts specifically.** P7's "Price Alerts" section has Active/Archived expanders. An empty Active box is a legitimate "no alerts armed" state, not a loading failure. Flag any stale archived alert (price has moved well past the old trigger) for manual review.

5. **Close the tab(s)** with `tabs_close_mcp` when done, unless David asked to keep the dashboard open.

6. **Write the EOD summary conversationally**, cross-referencing whatever technical/macro threads have been discussed in the conversation so far (harmonic patterns, other analysts'/agents' theses, cycle calls, bond moves, etc.) rather than just restating numbers. If a thesis or "recollection" is presented as prior shared context but doesn't actually appear earlier in this conversation, say so plainly rather than building on it as if confirmed — don't let an unverifiable secondhand claim quietly become part of the journal's record.

7. **Write the dated journal entry** using the naming convention and destination folder documented in the `trading-diary-location` skill — do not improvise a filename or path. That skill also covers requesting device-bridge folder access (per-session, never assumed to carry over) and delivering via `SendUserFile` + `device_commit_files`.

8. **Sync it into the pipeline.** Per the `journal-sync-workflow` skill, the journal only feeds the next Morning Brief once `journal_sync.py` has run — this is a manual step, not on any schedule. Always say so explicitly when you finish saving a journal entry (e.g. "saved to trading_diary — run journal_sync, or the P10 sidebar button, when you want this reflected in tomorrow's brief"), rather than implying it's already wired in.

## Notes / Gotchas

- Never assume the dashboard is unreachable just because a prior `WebFetch`/`Bash` attempt failed — that only proves a cloud sandbox's own network can't reach it. Try the Chrome-extension route first.
- Streamlit pages take a couple seconds to finish rendering after navigation.
- Preserve exact numbers (price, $ change, % change) — don't round or paraphrase figures David could act on.
- Don't stop at "delivered via SendUserFile" — confirm plainly whether it also landed in the real Dropbox `trading_diary` folder (per `trading-diary-location`), and whether `journal_sync` still needs to be run (per `journal-sync-workflow`). Those two facts are easy to let slide silently, and both matter to whether the entry actually does anything beyond sit in the chat.
