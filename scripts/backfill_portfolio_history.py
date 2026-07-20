"""Reusable backfill for a daily_sync outage gap — built for the 2026-07
outage (issue #67), designed to be rerun for any future gap via --start/--end.

This script assumes holdings were static across the gap — it reprices/flat-
carries the last real snapshot forward, it does not replay the ledger. That
assumption is checked automatically (see _check_holdings_static): any ledger
event with a real quantity change during the window aborts the run before any
data is touched, rather than silently carrying forward stale holdings across
a trade. See README.md's "Recovering from a multi-day sync outage" section.

When holdings are confirmed static, for each missing trading day:

  - EQ/ETF holdings are repriced using that day's yfinance raw (unadjusted)
    close — raw, not adjusted, because this is a balance-sheet valuation
    backfill (what the position was actually worth that day), not a
    total-return series.
  - Everything else (BOND, MUTUALFUND, MMF, and any unknown security_type) is
    flat-carried at its exact source-snapshot market_value. This isn't a
    fallback of convenience: money-market funds hold a constant ~$1 NAV by
    design, bonds aren't continuously quoted daily by any accessible API
    (E*TRADE's included — its Market Data API is real-time/intraday only, no
    historical-by-date endpoint), and CUSIP-identified bonds aren't valid
    yfinance tickers at all.
  - A nominally repriceable ticker (EQ/ETF) that yfinance can't actually
    price for the window falls back to flat-carry too, logged so it's
    visible — a holding's value is never silently dropped.

Writes only to `portfolio_value_backfill`; never touches `positions` (raw
E*TRADE truth) or anything in etrade_sync/.

Requires network access to yfinance — run from a machine that has it, not the
sandbox used for development.

Usage:
    python scripts/backfill_portfolio_history.py [--start 2026-07-01] [--end 2026-07-19] [--dry-run]
"""

import argparse
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))
from etrade_sync.db import get_connection

REPRICEABLE_SECURITY_TYPES = {"EQ", "ETF"}
RECONCILE_TOLERANCE_PCT = 0.0025   # 0.25% of the real snapshot total
RECONCILE_TOLERANCE_MIN_ABS = 25.0  # floor so tiny accounts aren't over-strict


def _trading_days(cur, start, end):
    """Trading days in range, taken from market_prices' SPY calendar — SPY has a
    row for every real trading day, so this naturally excludes weekends/holidays
    without hardcoding a market calendar."""
    cur.execute(
        "SELECT date FROM market_prices WHERE symbol = 'SPY' AND date BETWEEN %s AND %s ORDER BY date",
        (start, end),
    )
    return [row[0] for row in cur.fetchall()]


def _check_holdings_static(cur, start, end):
    """This script's whole approach assumes holdings didn't change during the
    gap — it reprices/flat-carries the source snapshot forward, it doesn't
    replay the ledger. Verify that assumption against the actual ledger rather
    than trusting the caller: any row with a non-null, non-zero quantity in
    the window (buy, sell, split, transfer, redemption, whatever the
    event_type) means a real share-count change happened, and this script
    would silently carry forward stale holdings across it. Returns the
    offending ledger rows (empty list if holdings were genuinely static)."""
    cur.execute(
        """
        SELECT event_timestamp, symbol, event_type, quantity
        FROM ledger
        WHERE event_timestamp::date BETWEEN %s AND %s
          AND quantity IS NOT NULL AND quantity != 0
        ORDER BY event_timestamp
        """,
        (start, end),
    )
    return cur.fetchall()


def _source_snapshot_holdings(cur, before_date):
    """Per-account, per-symbol quantity/market_value/security_type as of the
    latest positions snapshot strictly before before_date."""
    cur.execute(
        "SELECT MAX(fetched_at::date) FROM positions WHERE fetched_at::date < %s",
        (before_date,),
    )
    (snapshot_date,) = cur.fetchone()
    if snapshot_date is None:
        raise RuntimeError(f"No positions snapshot found before {before_date}")

    cur.execute(
        """
        SELECT p.account_id_key, p.symbol, p.quantity, p.market_value, p.security_type
        FROM positions p
        JOIN (
            SELECT account_id_key, MAX(fetched_at) AS latest_ts
            FROM positions
            WHERE fetched_at::date = %s
            GROUP BY account_id_key
        ) latest ON latest.account_id_key = p.account_id_key AND latest.latest_ts = p.fetched_at
        WHERE p.quantity IS NOT NULL AND p.quantity != 0
        """,
        (snapshot_date,),
    )
    cols = ["account_id_key", "symbol", "quantity", "market_value", "security_type"]
    holdings = [dict(zip(cols, row)) for row in cur.fetchall()]
    return snapshot_date, holdings


def _historical_closes(symbols, start, end):
    """symbol -> {date: raw_close}, via yfinance (auto_adjust=False — raw close,
    not adjusted, per the balance-sheet-valuation rationale above). Best-effort
    per symbol — one yfinance can't price at all comes back with an empty dict
    and is flagged, not a hard failure."""
    closes = {}
    end_exclusive = end + timedelta(days=1)  # yfinance's end= is exclusive
    for symbol in symbols:
        try:
            hist = yf.Ticker(symbol).history(start=start, end=end_exclusive, auto_adjust=False)
            closes[symbol] = {ts.date(): float(row["Close"]) for ts, row in hist.iterrows()}
            if not closes[symbol]:
                print(f"  WARNING: yfinance returned no history at all for {symbol} — will flat-carry")
        except Exception as e:
            print(f"  WARNING: could not fetch history for {symbol}: {e} — will flat-carry")
            closes[symbol] = {}
        time.sleep(0.3)
    return closes


def _price_on(symbol_closes, day):
    """Exact close for `day`, or the most recent known close on/before it —
    handles a yfinance gap for a single day without losing the position."""
    if day in symbol_closes:
        return symbol_closes[day]
    known_dates = sorted(d for d in symbol_closes if d <= day)
    return symbol_closes[known_dates[-1]] if known_dates else None


def _compute_day_values(day, holdings, closes, flat_carry_tickers):
    """Returns {account_id_key: (total, repriced_part, flat_carry_part)} for one day."""
    per_account = defaultdict(lambda: [0.0, 0.0, 0.0])  # total, repriced, flat_carry
    for h in holdings:
        acct = h["account_id_key"]
        if h["symbol"] in flat_carry_tickers:
            value = float(h["market_value"])
            per_account[acct][0] += value
            per_account[acct][2] += value
            continue

        price = _price_on(closes.get(h["symbol"], {}), day)
        if price is None:
            # Nominally repriceable ticker with no usable history for this
            # window at all — flat-carry it rather than drop its value.
            value = float(h["market_value"])
            per_account[acct][0] += value
            per_account[acct][2] += value
            continue

        value = float(h["quantity"]) * price
        per_account[acct][0] += value
        per_account[acct][1] += value

    return per_account


def backfill(start, end, dry_run=False):
    # Coerce here too, not just in the CLI arg parser — any future caller
    # passing plain "YYYY-MM-DD" strings would otherwise crash later in
    # _historical_closes()'s end + timedelta(days=1).
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            trade_activity = _check_holdings_static(cur, start, end)
            if trade_activity:
                print(f"ABORTING — holdings were NOT static during {start} to {end}. "
                      f"This script only reprices/flat-carries a static snapshot forward; "
                      f"it does not replay the ledger. Found {len(trade_activity)} ledger "
                      f"event(s) with a real quantity change:")
                for event_timestamp, symbol, event_type, quantity in trade_activity:
                    print(f"  {event_timestamp}  {symbol:<12} {event_type:<20} qty={quantity}")
                print("Do not run this backfill as-is for this window — it would silently "
                      "carry forward stale holdings across a real trade. Needs a ledger-replay "
                      "enhancement first (see README.md's 'Recovering from a multi-day sync "
                      "outage' section).")
                return

            snapshot_date, holdings = _source_snapshot_holdings(cur, start)
            trading_days = _trading_days(cur, start, end)

        # Derived from the same `holdings` list used everywhere else in this
        # script (already filtered to the latest fetched_at per account on
        # snapshot_date) rather than a second, independently-filtered query —
        # positions can have multiple intraday snapshots per day (see
        # mv_portfolio_timeseries.sql's own "latest snapshot per day" comment),
        # so a plain SUM(market_value) over all rows for the date would double
        # count and false-abort (or wrongly pass) the reconciliation check below.
        real_snapshot_totals = defaultdict(float)
        for h in holdings:
            real_snapshot_totals[h["account_id_key"]] += float(h["market_value"])

        if not trading_days:
            print(f"No trading days found in market_prices between {start} and {end} — nothing to backfill.")
            return
        if not holdings:
            print(f"No holdings found in the {snapshot_date} snapshot — nothing to backfill.")
            return

        flat_carry_tickers = {h["symbol"] for h in holdings if h["security_type"] not in REPRICEABLE_SECURITY_TYPES}
        repriceable_tickers = {h["symbol"] for h in holdings if h["security_type"] in REPRICEABLE_SECURITY_TYPES}

        print(f"Source snapshot: {snapshot_date}. {len(holdings)} holdings across {len({h['account_id_key'] for h in holdings})} accounts.")
        print(f"  Repriceable (EQ/ETF): {sorted(repriceable_tickers)}")
        print(f"  Flat-carry (BOND/MUTUALFUND/MMF/unknown): {sorted(flat_carry_tickers)}")

        closes = _historical_closes(sorted(repriceable_tickers), start, end)
        # A repriceable ticker with genuinely no history anywhere falls back to
        # flat-carry for every day, not just days with a gap.
        for symbol in repriceable_tickers:
            if not closes.get(symbol):
                flat_carry_tickers.add(symbol)

        # Safeguard: reconstruct the source snapshot's own total with the same
        # formula and verify it reconciles before trusting it for the gap days.
        snapshot_values = _compute_day_values(snapshot_date, holdings, closes, flat_carry_tickers)
        reconcile_failures = []
        for acct, (total, _, _) in snapshot_values.items():
            real_total = float(real_snapshot_totals.get(acct, 0.0))
            tolerance = max(RECONCILE_TOLERANCE_MIN_ABS, abs(real_total) * RECONCILE_TOLERANCE_PCT)
            if abs(total - real_total) > tolerance:
                reconcile_failures.append((acct, total, real_total, tolerance))

        if reconcile_failures:
            print("ABORTING — reconstructed source-snapshot total does not reconcile with the real snapshot:")
            for acct, computed, real, tolerance in reconcile_failures:
                print(f"  {acct}: computed={computed:.2f} real={real:.2f} tolerance={tolerance:.2f}")
            return

        print("Reconciliation OK — reconstructed source-snapshot total matches the real snapshot within tolerance.")

        rows = []
        ticker_repriced_total = defaultdict(float)
        ticker_flat_carry_total = defaultdict(float)
        for day in trading_days:
            day_values = _compute_day_values(day, holdings, closes, flat_carry_tickers)
            for acct, (total, repriced, flat_carry) in day_values.items():
                rows.append((acct, day, round(total, 2), round(repriced, 2), round(flat_carry, 2), snapshot_date))

        for h in holdings:
            bucket = ticker_flat_carry_total if h["symbol"] in flat_carry_tickers else ticker_repriced_total
            # Rough per-ticker magnitude for the summary — not per-day precision,
            # just enough to see where the dollars are concentrated.
            bucket[h["symbol"]] += float(h["market_value"]) * len(trading_days)

        print("\nRepriced vs. flat-carry summary (approx. $ across the whole window):")
        for symbol in sorted(set(ticker_repriced_total) | set(ticker_flat_carry_total)):
            mode = "repriced" if symbol in ticker_repriced_total else "flat-carry"
            amount = ticker_repriced_total.get(symbol) or ticker_flat_carry_total.get(symbol)
            print(f"  {symbol:<12} {mode:<11} ~${amount:,.0f}")

        if dry_run:
            print(f"\nDry run — {len(rows)} (account, date) rows would be upserted, no changes made.")
            return

        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO portfolio_value_backfill
                    (account_id_key, date, total_market_value, repriced_market_value,
                     flat_carry_market_value, source_snapshot_date)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id_key, date) DO UPDATE SET
                    total_market_value      = EXCLUDED.total_market_value,
                    repriced_market_value   = EXCLUDED.repriced_market_value,
                    flat_carry_market_value = EXCLUDED.flat_carry_market_value,
                    source_snapshot_date    = EXCLUDED.source_snapshot_date
                """,
                rows,
            )
        conn.commit()
        print(f"\nBackfilled {len(rows)} (account, date) rows into portfolio_value_backfill.")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2026-07-01", type=date.fromisoformat, help="First date to backfill (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-07-19", type=date.fromisoformat, help="Last date to backfill (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be upserted without writing")
    args = parser.parse_args()
    backfill(args.start, args.end, dry_run=args.dry_run)
