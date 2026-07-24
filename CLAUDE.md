# Fifth Dragon Capital — Claude Context
_Last updated: July 24, 2026. Read this at the start of every session._

---

## Project Overview
Personal trading infrastructure. PostgreSQL DB + Python + Streamlit dashboard.
Owner: David Liao (david.liao@precisetarget.com)

**Workflow:** short-lived `feature/*` or `fix/*` branches off `main`, one PR per change, merge immediately once verified. Don't commit directly to `main`.
**Git identity for commits:** always use `-c user.email="david.liao@precisetarget.com" -c user.name="David Liao"`
**morning_brief WIP:** the Confirmation Hierarchy feature (#66) Part 1 lives on branch `update_morning_brief1` (committed, pushed, NOT merged into main) — has a known blocking bug, see "Known open items" below. Don't build Part 2 on top of it until that's fixed.

---

## Directory Structure

```
fifth_dragon_capital/
├── alerts/              # existing — DO NOT MODIFY
├── dashboard/
│   └── pages/
│       ├── P10_Morning_Brief.py  # Streamlit page — user-built, Claude added sync features
│       └── P1-P9                # other actively-maintained pages (Pipeline Status, Portfolio
│                                 # Overview, Physical Metals, Market Monitor, Commodities, etc.)
│                                 # — not just morning_brief scope; see git log for recent changes
├── etrade_sync/         # existing — DO NOT MODIFY
├── morning_brief/       # NEW module — all new work goes here
│   ├── __init__.py
│   ├── key_levels.yml   # user metadata: stops, notes, watch levels
│   ├── fetchers.py      # data fetching + DB sync
│   ├── formatter.py     # markdown renderer
│   └── brief.py        # main entrypoint: python -m morning_brief.brief
├── scripts/
│   ├── morning_brief.sh                      # shell wrapper for launchd
│   └── com.fifthdragon.morning-brief.plist   # launchd job — 6:45am daily
└── logs/                # created at runtime
```

---

## Architecture — Two-Layer Position Data

1. **PostgreSQL DB (`mv_unrealized_pnl`)** — authoritative source for tickers + cost basis + P&L
2. **`morning_brief/key_levels.yml`** — user metadata layer: stops, notes, watch levels

`sync_positions_from_db()` smart merge rules:
- Add new tickers from DB not in YAML
- Remove closed positions ONLY if they have no stop AND no note
- Preserve all user stops/notes always

---

## Key Functions (fetchers.py)

| Function | Purpose |
|----------|---------|
| `fetch_positions_from_db()` | Queries `mv_unrealized_pnl` via `etrade_sync.db.get_connection()` |
| `sync_positions_from_db(key_levels)` | Smart merge. Returns `(updated_kl, added, removed)` |
| `fetch_positions(key_levels)` | DB-first, merges YAML metadata, falls back to YAML-only |
| `fetch_watch_levels(key_levels)` | Computes distance to support/resistance |
| `_fetch_snapshot(symbols)` | Batch yfinance download with per-ticker error handling |
| `fetch_global_indices()` | Calls `_fetch_snapshot` |
| `fetch_us_futures()` | Calls `_fetch_snapshot` |
| `fetch_commodities()` | Calls `_fetch_snapshot` — note: uranium uses `"URA"` (not `"UX=F"`) |
| `fetch_currencies()` | Calls `_fetch_snapshot` |
| `fetch_vol_proxies()` | Calls `_fetch_snapshot` |
| `fetch_fed_events()` | Reads `events.yml` if present |

---

## Known open items (as of 2026-07-24)

- **`events.yml` still doesn't exist** — the FOMC/data-release events section in the Morning Brief is silent without it.
- **Confirmation Hierarchy (#66) Part 1 has a blocking bug**, on branch `update_morning_brief1`: `range_expansion`/`move_percentile`/`crossed_level` compute off yesterday's completed daily bar instead of the current price, inconsistent with `overnight_return`/`dist_from_*dma` which correctly use the fresher price. Fix before any Part 2 scoring work — see issue #66.
- **Alert system (#63) has 3 deferred, unscoped items**: a dismissal/acknowledgment mechanism for reviewed stale alerts, a decision on auto-archive vs. permanent review-only for stale detection, and a `key_levels.updated_at`-based freshness check for structural rows. None blocking, just not built yet.
- **`sync-daily` (the 6am launchd job) fails every morning by design** — E*TRADE tokens expire at midnight ET, so an unattended 6am job can never have a fresh token. This is accepted, not a bug (see commit fd27d9a). To sync positions without the full Morning Pipeline, use P1 Pipeline Status → Run Jobs → `sync` (data type: positions) after re-authenticating.
- **No market buy order capability exists** — only `preview_market_sell`/`place_market_sell` are implemented in `etrade_sync/trading/orders.py`. A buy would mirror the sell implementation's shape.
- **Live E*TRADE order placement is currently enabled** (`ETRADE_DEV=false`, `ETRADE_LIVE_ORDERS=true` in `.env`) — be careful with anything touching `etrade_sync/trading/orders.py` or P2's Quick Sell panel; it hits the real account, not sandbox.

Resolved since the last pass (previously listed here as pending, now done): `load_key_levels_from_db()`/`save_key_levels_to_db()` are implemented in `fetchers.py`; `pyyaml` is in `requirements.txt`; `morning_brief/journal_sync.py` and the `journal_sync_log` table both exist.

---

## P10_Morning_Brief.py — What It Does

Streamlit page at `dashboard/pages/P10_Morning_Brief.py`. User-built, Claude added features.

**Tabs:**
- **Tab 1 (Brief):** Renders `trading_diary/morning_brief.md` (output of `python -m morning_brief.brief`)
- **Tab 2 (Key Levels):** Editable UI for positions (stops/notes) and watch levels. DB snapshot read-only table at top. Save All button syncs `alert_above` values to `price_alerts` DB table.
- **Tab 3 (Sync History):** Reads `journal_sync_log` table.

**Sidebar buttons:**
- ▶ Run Morning Brief — runs `python -m morning_brief.brief`
- 🔄 Sync Positions from DB — calls `sync_positions_from_db()` smart merge
- 🔄 Sync Latest Journal — calls `python -m morning_brief.journal_sync --file <latest>`
- 🔄 Sync All Journals — calls `python -m morning_brief.journal_sync`

---

## launchd Setup

- **Script:** `scripts/morning_brief.sh`
- **Plist:** `scripts/com.fifthdragon.morning-brief.plist`
- **Schedule:** 6:45am daily (after `sync-daily` at 6:00am refreshes positions)
- **Python:** `/Users/davidliao/git_repos/py312/venv/bin/python`
- **Output:** `~/Library/CloudStorage/Dropbox/Etrade/trading_diary/morning_brief.md`
- **Logs:** `logs/launchd_morning_brief.log` + `logs/launchd_morning_brief_error.log`

**To install:**
```bash
cp scripts/com.fifthdragon.morning-brief.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fifthdragon.morning-brief.plist
```

### Alert Poller

- **Script:** `scripts/alert_poller.sh`
- **Plist:** `scripts/com.fifthdragon.alert-poller.plist`
- **Schedule:** every 5 minutes, runs immediately on load too (`RunAtLoad`)
- **Runs:** `python -m alerts.poller --once` — fires/re-arms `price_alerts` rows
- **Logs:** `logs/alert_poller.log` (script's own output) + `logs/launchd_alert_poller.log`/`_error.log` (launchd, usually empty since the script redirects its own output)

Added because the poller previously only ran via the manual "▶ Run Alert Poll" button in P7 — alerts could cross their threshold and never fire since nothing was checking prices in the background. `alerts/poller.py` itself is untouched, per the do-not-modify rule — this is purely an operational wrapper.

**To install:**
```bash
cp scripts/com.fifthdragon.alert-poller.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fifthdragon.alert-poller.plist
```

---

## DB Connection

Via `etrade_sync.db.get_connection()`. Key table: `mv_unrealized_pnl` (materialized view).

Columns used from `mv_unrealized_pnl`:
- `symbol`, `quantity`, `cost_basis`, `market_value`, `unrealized_pnl`, `unrealized_pnl_pct`

---

## key_levels.yml Schema

```yaml
positions:
  TICKER:
    stop: 54.95        # float, optional
    note: "text"       # string, optional

watch:
  TICKER:
    support: 4200.0    # float, optional
    resistance: 4489.0 # float, optional
    alert_above: 18.0  # float, optional — synced to price_alerts DB table on Save
    note: "text"       # string, optional
```

---

## Network Note

As of 2026-07-24, the sandbox (bash tool) can reach GitHub over HTTPS directly — `git push`/`pull` and `gh pr create`/`merge` all work without needing the Mac terminal — and yfinance fetches succeed too. Older versions of this note said otherwise; if a session ever hits 403s or auth failures again, fall back to asking the user to run the command from their Mac terminal rather than assuming this is permanently fixed.

---

## Trading Context
See `/Users/davidliao/Library/CloudStorage/Dropbox/Etrade/trading_diary/SESSION_CONTEXT.md` for full trading state: positions, stops, active theses, key calendar events, trading rules.

Daily journal files live in that same folder, named `trading_journal_<month><day>_<year>.md` (e.g. `trading_journal_july13_2026.md`, no leading zeros, month spelled out lowercase). `morning_brief/journal_sync.py` globs `trading_journal_*.md` there to extract positions/watch levels/alerts — keep new journal files matching this pattern or the sync won't pick them up.
