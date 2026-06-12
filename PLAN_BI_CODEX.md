# PLAN_BI_CODEX

Working memory for Codex on the BI project. This file is intentionally small and
practical: it records the current branch context, what is already done in code,
and what should be tackled next.

## Current Context

- Repo: `fifth_dragon_capital`
- Active branch for current work: `feature/issue_39`
- Baseline `main` already includes:
  - transaction provenance + ingest audit
  - canonical ledger rebuild
  - split-adjusted FIFO realized P/L
  - portfolio/performance/trading-history dashboard pages
  - manual lot provenance flags in Portfolio Overview
  - exposure tagging / symbol overrides

## Completed In Code

- `#22` `mv_portfolio_timeseries`
- `#23` `mv_allocations`
- `#24` benchmark comparison vs SPY
- `#25` Portfolio Overview
- `#26` Performance page
- `#27` Trading History page
- `#29` partially implemented:
  - `trade_tags` table exists
  - tag form exists in Trading History
  - tag relinking on FIFO rebuild exists
- `#32` split-adjusted FIFO
- `#36` transaction provenance / duplicate audit trail
- `#37` canonical ledger rebuild
- `#38` provenance flags in lot detail UI are implemented, but the original review/dismiss scope remains broader than the current code

## Open / Next

- `#28` Risk & Exposure page
- `#29` strategy performance charts and taxonomy UX completion
- `#33` backfill `cusip`
- `#34` options / multi-leg order mapping
- `#35` OAuth re-auth UI

## Current Design Notes

- Keep `trade_tags -> realized_gains.id` for now; it is stronger than a generic
  `source_table/source_id` shape for this codebase.
- For `#29`, split work into:
  - Pass 1: strategy summary view + P4 charts + taxonomy dropdown
  - Pass 2: capital deployed over time + P3 breakout
- For `#28`, focus on sector concentration, position sizing, largest losing
  trades, holding-period breakdown, and options exposure.

## Risk Page Notes

- The page should feel like portfolio diagnosis, not just allocation reporting.
- Display percentages in `#28` using absolute exposure where it improves clarity,
  so holdings like cash equivalents and hedges do not collapse to misleading
  near-zero values.
- Show both:
  - share of total portfolio
  - share of invested assets
- Preferred sections for the first useful version:
  - Top KPI strip
  - Vulnerabilities
  - Concentration / Exposure
  - Correlated Holdings
  - Stress Scenarios later
- Suggested vulnerability examples:
  - largest position exceeds threshold
  - precious metals concentration
  - commodity-heavy exposure
  - no leverage detected
  - cash reserve above target
- Suggested interpretation-first metrics:
  - diversification score
  - invested capital / risk-on allocation
  - exposure relative to invested capital
- Future additions to revisit later:
  - scenario analysis
  - correlation clusters
  - historical risk trend

## Future Risk Page Redesign Note

- The mockup direction is strong but should be treated as a future redesign,
  not a same-day patch.
- Future look-and-feel direction:
  - premium wealth-management feel rather than brokerage reporting
  - dark, polished dashboard presentation
  - top-level vulnerability strip with concise cards
  - diagnosis-oriented framing instead of raw allocation charts
- Suggested future strip content:
  - Largest position
  - Cash reserve
  - Diversification score
  - No leverage detected
  - Intentional commodity tilt
- Suggested future layout:
  - vulnerabilities strip at top
  - concentration / exposure bars beneath
  - sizing table and loss watch below
- Keep this as a later redesign pass so the current implementation can stay
  incremental and low-risk.

## Absolute-Weight Rule

- For display percentages on allocation/risk pages, use absolute market value
  where the intention is to show exposure magnitude.
- Keep a separate signed value only when the sign itself is meaningful.
- If a visible table or chart is meant to sum to 100%, recompute it from the
  currently displayed rows and add a warning if it does not reconcile.

## Working Rule

- Read the issue first, then inspect current `main`, then plan the smallest
  useful implementation slice that fits the existing code.
