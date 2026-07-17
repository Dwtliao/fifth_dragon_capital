# Morning Brief Market Status Framework

## Purpose

Turn Morning Brief from a raw market-data roundup into a decision support layer that answers:

1. What is leading?
2. What is confirming?
3. What is diverging?
4. What is still quiet?

The goal is not to classify instruments by simple price change. The goal is to identify unusual behavior relative to each instrument's own history and use that to describe market regime.

## Core Principle

Do not mark an instrument as stressed just because it moved a lot.
Mark it as stressed when it is behaving unusually relative to its own expected range, volatility, volume, or key levels.

## Framework

### 1. Observation

Collect normalized facts first, before interpretation:

- return vs prior close
- move vs ATR or realized volatility
- true range vs ATR20
- support/resistance or moving-average breaks
- volume vs normal volume
- overnight vs cash-session behavior
- persistence after the initial move

### 2. Instrument Status

Classify each instrument into one of four states:

- Quiet
- Active
- Stressed
- Extreme

Suggested mapping:

- 0-1 = Quiet
- 2-3 = Active
- 4-6 = Stressed
- 7+ = Extreme

### 3. Market Role

Classify the instrument's role in the current tape:

- Leading
- Confirming
- Diverging
- Isolated

This is separate from status.

- Leading: moves first, before the broader market confirms.
- Confirming: follows leadership.
- Diverging: conflicts with the expected regime.
- Isolated: specific to a name or niche group without broader confirmation.

## Design Notes

### Use normalized scoring, not raw thresholds

The hard part is not fetching tickers. The hard part is deciding what counts as stressed.

A simple percent move threshold is fast, but it is too blunt for the kinds of signals the brief is trying to surface.

A better model is:

- compute a few objective features
- score them
- map the score to status
- assign role separately

### Prefer relative behavior over absolute movement

Examples:

- SOXS moving 3 percent is not automatically meaningful.
- SOXS moving 1.8 ATRs, breaking a key level, and holding with volume is meaningful.

### Separate status from regime

An instrument can be:

- Quiet but diverging
- Active but confirming
- Stressed but isolated

That separation is useful because a quiet divergence can be more informative than a noisy but obvious move.

## Suggested First Version

Use a three-layer hierarchy:

### Level 1 - Leadership

Examples:

- SOXS
- SOXL
- SMH
- MU
- Kioxia proxy, if reliable

These are early warning indicators.

### Level 2 - Index Confirmation

Examples:

- Nasdaq
- Nikkei
- SOX
- S&P 500

These show whether leadership weakness is spreading.

### Level 3 - Macro Confirmation

Examples:

- VIX
- VVIX
- DXY
- credit-spread proxy

These show whether the move is becoming systemic.

## Data Strategy

### Data sources

Reuse the existing Morning Brief fetch pattern where possible:

- `morning_brief/fetchers.py` for snapshot fetches
- `morning_brief/formatter.py` for rendering
- `morning_brief/brief.py` for sequencing the sections

### Keep the first version practical

Not every concept needs a perfect data source on day one.

Recommended starting point:

- use available yfinance tickers
- use futures where possible for overnight leadership
- use a proxy for credit spreads before adding a new market data integration

## Implementation Shape

Add a new section to Morning Brief:

- `render_market_status(...)`

The section should be compact and summarized, not a raw price table.

For each instrument, show:

- status
- role
- confidence
- short reasons

Example:

- `SOXS: Stressed / Leading / High`
- reasons:
  - 97th percentile overnight move
  - 1.8 ATR range expansion
  - broke 20-day high
  - volume 2.1x normal

## Open Decisions

These should be decided before implementation:

1. Do we score status primarily from percent move, ATR expansion, or key-level breaks?
2. Do we treat overnight/futures behavior as part of the score by default?
3. Do we want instrument-specific rules in `key_levels.yml`, or a generic scoring model with per-instrument overrides?
4. What is the cheapest acceptable proxy for credit spreads?

## Recommendation

Build this as a synthesis layer, not as another market dump.

The output should answer:

- what moved first
- what confirmed
- what is diverging
- what is still quiet

That makes the brief useful for decisions instead of just informative.
