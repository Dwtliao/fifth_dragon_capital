# Portfolio Broker-Lot Reconciliation Plan

## Objective

Make E*TRADE's current open tax lots the authoritative source for the dashboard's
Position Lot Detail. Retain existing FIFO-derived lots only for local historical
analytics and clearly labeled fallback/audit use.

## Problem Observed

P2 currently reconstructs open lots from transactions using FIFO. That preserves
the correct aggregate position quantity, but it cannot know when a sell used
specific tax lots selected at E*TRADE.

For NFGC in the Rollover IRA, the August 25, 2026 sale produced this difference:

| Source | Remaining lots |
|---|---|
| Current local FIFO reconstruction | 200 shares from 2025-04-30, 500 from 2026-05-21, 350 from 2026-07-22, and 450 from 2026-07-24 |
| E*TRADE broker lots | 700 shares from 2025-04-30, 350 from 2026-07-22, and 450 from 2026-07-24 |

Both total 1,500 shares. The difference exists because the broker applied the
sale to the selected 500-share 2026-05-21 lot, whereas local FIFO consumed the
oldest shares.

## Source-of-Truth Rules

| Situation | P2 Position Lot Detail source |
|---|---|
| E*TRADE returns complete current broker lots matching its aggregate position quantity | E*TRADE broker lots, shown as authoritative |
| E*TRADE returns no broker lots for an active position | Local FIFO/CSV/manual lots, explicitly labeled **Estimated local lots** |
| E*TRADE lots do not total the aggregate E*TRADE position quantity | Aggregate position plus a visible incomplete-lot warning; do not silently substitute or merge local lots |

- Broker-lot data will be a separate append-only E*TRADE-derived source. It will
  not overwrite raw transactions, the ledger, FIFO `realized_gains`, or FIFO
  `open_lots`.
- Broker lots and local FIFO/CSV/manual lots must never be merged for one
  account/symbol display. A selected-lot sale is expected to make FIFO differ.
- Broker lots are authoritative for current open-lot composition only. Historical
  specific-lot dispositions can be captured from future snapshots but may not be
  reconstructable for periods before collection began.

## Implementation Checklist

- [x] Confirm the exact `lotsRequired=true` / position-lot API response shape using
  a valid E*TRADE token, including identifiers, dates, quantities, and cost fields.
- [x] Add an append-only `broker_position_lots` snapshot table with account, symbol,
  position/lot identifiers, acquisition date, open quantity, cost basis/price,
  raw payload, and a snapshot identifier/timestamp shared with the aggregate
  `positions` snapshot.
- [x] Extend the positions sync to request and persist broker lots with the same
  snapshot identifier as their aggregate E*TRADE position. A lot-fetch failure
  must be reported but must not discard the aggregate position snapshot.
- [x] Implement only a quantity-coverage integrity check:
  `SUM(broker lot quantity) = aggregate E*TRADE position quantity` per
  account/symbol/snapshot. Do not reconcile individual broker lots to FIFO.
- [x] Update P2 Position Lot Detail to display complete E*TRADE broker lots by
  default, with the local FIFO source retained for analytics/audit and used only
  as an explicitly labelled fallback when broker lots are unavailable.
- [x] When broker lots are missing or incomplete, use a clearly labeled local
  fallback or show an incomplete-lot warning; never mix the two sources.
- [x] Add automated fixtures/tests for selected-lot sales, including NFGC's expected
  three broker lots totaling 1,500 shares while the FIFO view remains distinct.
- [ ] Add tests for broker-lot request-error handling and coverage statuses.
- [x] Run the migration and a safe verification sync with a valid token; verify
  Rollover IRA NFGC displays the three E*TRADE lots: 700 (2025-04-30), 350
  (2026-07-22), and 450 (2026-07-24).
- [ ] Update project documentation and this plan with implementation decisions,
  verification results, and any API limitations discovered.

## Progress Log

### 2026-08-26 — Planning

- Confirmed that E*TRADE's raw position payload contains a `lotsDetails` link.
- Confirmed the installed `pyetrade` client supports both `lots_required=True` and
  `get_portfolio_position_lot(symbol, account_id_key)`.
- Confirmed the local NFGC aggregate position, current materialized views, and
  FIFO lots all reconcile to 1,500 Rollover IRA shares.
- Revised the design to a broker-first model: E*TRADE lots win whenever their
  quantities cover the current E*TRADE position; FIFO is not reconciled to or
  merged with broker-selected lots.
- Added migration 073 (`broker_position_lots`) and 074 (`broker_lot_coverage`).
- Updated the positions sync to capture active equity lots through the E*TRADE
  `lotsDetails` endpoint, without changing transactions, the ledger, or FIFO lots.
- Updated P2 to prefer complete E*TRADE broker lots and to label local FIFO data
  as a fallback.
- Applied the migration, completed a positions-only Rollover IRA sync, and refreshed
  derived views. E*TRADE returned NFGC lots of 700 / 350 / 450; quantity coverage
  is complete at 1,500 / 1,500 shares.
- Created a fresh database backup before further verification:
  `fifth_dragon_capital_20260826_111630.dump`.
- Changed individual broker-lot HTTP failures to non-fatal sync warnings. Verified
  against E*TRADE's persistent 500 for Trading Account `G0137D118`: aggregate
  positions and available broker lots still sync successfully, while the affected
  symbol remains eligible for the labelled local fallback.
- Fixed P2's broker-lot query after live verification exposed an ambiguous
  `account_id_key` join. The corrected query returns 77 covered broker lots,
  including Rollover IRA NFGC at 700 / 350 / 450 shares.
- Automated tests: 47 passing.
