# Fifth Dragon Capital — Claude Context
_Last updated: June 18, 2026. Read this at the start of every session._

---

## Project Overview
Personal trading infrastructure. PostgreSQL DB + Python + Streamlit dashboard.
Owner: David Liao (david.liao@precisetarget.com)

**Active branch:** `feature/morning_brief1`
**Push when done:** `git push --set-upstream origin feature/morning_brief1`
**Git identity for commits:** always use `-c user.email="david.liao@precisetarget.com" -c user.name="David Liao"`

---

## Directory Structure

```
fifth_dragon_capital/
├── alerts/              # existing — DO NOT MODIFY
├── dashboard/
│   └── pages/
│       └── P10_Morning_Brief.py  # Streamlit page — user-built, Claude added sync features
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

## KNOWN BUG — MUST FIX

**`brief.py` calls `fetchers.load_key_levels_from_db()` — this function does not exist in fetchers.py.**

User modified `brief.py` to call a DB-backed key levels loader, but the function was never implemented. The original function was `_load_key_levels()` (reads from YAML file).

**Fix needed:** Implement `load_key_levels_from_db()` and `save_key_levels_to_db()` in `fetchers.py`.

These functions are also called in `P10_Morning_Brief.py`:
```python
from morning_brief.fetchers import (
    fetch_positions_from_db, sync_positions_from_db,
    load_key_levels_from_db, save_key_levels_to_db,
)
```

**Decision needed:** Store key_levels in DB (new table) or keep in YAML? P10 currently uses DB-backed versions. The simplest fix is to implement these as YAML read/write wrappers so the interface matches what P10 expects.

---

## PENDING TASKS

1. **Implement `load_key_levels_from_db()` / `save_key_levels_to_db()`** in fetchers.py (see bug above)
2. **Create `events.yml`** with upcoming FOMC/data release dates (events section in brief is silent without it)
3. **Add `pyyaml` to `requirements.txt`** (used throughout morning_brief but not in requirements)
4. **Create `morning_brief/journal_sync.py`** — P10 sidebar has "Sync Latest Journal" button that calls this module; it doesn't exist yet
5. **Create `journal_sync_log` DB table** — P10 Tab 3 queries this table; schema needed
6. **Fix `ETRADE_DEV=true`** in .env — currently pointed at sandbox, not live E*TRADE
7. **Git push from Mac terminal** (sandbox can't auth to GitHub over HTTPS):
   ```
   git push --set-upstream origin feature/morning_brief1
   ```

---

## P10_Morning_Brief.py — What It Does

Streamlit page at `dashboard/pages/P10_Morning_Brief.py`. User-built, Claude added features.

**Tabs:**
- **Tab 1 (Brief):** Renders `trading_diary/morning_brief.md` (output of `python -m morning_brief.brief`)
- **Tab 2 (Key Levels):** Editable UI for positions (stops/notes) and watch levels. DB snapshot read-only table at top. Save All button syncs `alert_above` values to `price_alerts` DB table.
- **Tab 3 (Sync History):** Reads `journal_sync_log` table (DB table not yet created)

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

Sandbox (bash tool) cannot authenticate to GitHub over HTTPS. All `git push` commands must be run from the Mac terminal. Always include the push command in output so user can copy-paste:
```
git push --set-upstream origin feature/morning_brief1
```

Sandbox also blocks outbound network (yfinance fetches return 403). Code is structurally correct — yfinance works on Mac.

---

## Trading Context
See `/Users/davidliao/Library/CloudStorage/Dropbox/Etrade/trading_diary/SESSION_CONTEXT.md` for full trading state: positions, stops, active theses, key calendar events, trading rules.
