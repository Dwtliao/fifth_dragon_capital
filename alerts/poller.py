"""
Price alert poller — run as a standalone process:

    python -m alerts.poller              # poll every 5 minutes indefinitely
    python -m alerts.poller --once       # single poll and exit (useful for cron)
    python -m alerts.poller --interval 300

Reads alert definitions from the price_alerts DB table.
Fetches prices via yfinance. Sends email via alerts.notify.
"""
import argparse
import sys
import time
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))

from alerts.notify import send_alert_email
from etrade_sync.db import get_connection

DEFAULT_INTERVAL = 300  # 5 minutes


def _load_alerts(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, ticker, label, condition, threshold::float, triggered
            FROM price_alerts
            WHERE enabled = TRUE
            ORDER BY id
        """)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Returns {ticker: last_price} for each ticker that returned data."""
    prices = {}
    for ticker in tickers:
        try:
            price = yf.Ticker(ticker).fast_info.last_price
            if price:
                prices[ticker] = float(price)
        except Exception:
            pass
    return prices


def _set_triggered(conn, alert_id: int, triggered: bool):
    with conn.cursor() as cur:
        if triggered:
            cur.execute("""
                UPDATE price_alerts
                SET triggered = TRUE, last_fired_at = NOW()
                WHERE id = %s
            """, (alert_id,))
        else:
            cur.execute("""
                UPDATE price_alerts SET triggered = FALSE WHERE id = %s
            """, (alert_id,))
    conn.commit()


def run_once() -> int:
    """Run one poll cycle. Returns number of alerts fired."""
    conn = get_connection()
    try:
        alerts = _load_alerts(conn)
        if not alerts:
            print("No enabled alerts.")
            return 0

        tickers = list({a["ticker"] for a in alerts})
        print(f"Prices via yfinance (~15min delayed) — no E*TRADE connection required")
        print(f"Polling {len(tickers)} ticker(s): {', '.join(tickers)}")
        prices = _fetch_prices(tickers)

        fired = 0
        for alert in alerts:
            ticker    = alert["ticker"]
            price     = prices.get(ticker)
            if price is None:
                print(f"  {ticker}: no price data — skipping")
                continue

            threshold = alert["threshold"]
            condition = alert["condition"]
            condition_met = (price > threshold) if condition == "above" else (price < threshold)
            sign = ">" if condition == "above" else "<"

            if condition_met and not alert["triggered"]:
                print(f"  FIRE  {ticker} {price:,.4f} {sign} {threshold:,.2f}  [{alert['label'] or ''}]")
                send_alert_email(ticker, alert["label"] or "", condition, threshold, price)
                _set_triggered(conn, alert["id"], True)
                fired += 1
            elif not condition_met and alert["triggered"]:
                print(f"  REARM {ticker} {price:,.4f} (was triggered, now {sign[::-1]} threshold)")
                _set_triggered(conn, alert["id"], False)
            else:
                state = "triggered" if alert["triggered"] else "armed"
                print(f"  OK    {ticker} {price:,.4f}  [{state}]")

        return fired
    finally:
        conn.close()


def run_loop(interval: int):
    print(f"Price alert poller started — interval {interval}s. Ctrl+C to stop.")
    while True:
        print(f"\n{'─' * 50}")
        try:
            fired = run_once()
            print(f"Poll complete — {fired} alert(s) fired.")
        except Exception as e:
            print(f"Poll error: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Price alert poller")
    parser.add_argument("--once",     action="store_true", help="Run one poll and exit")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Poll interval in seconds (default {DEFAULT_INTERVAL})")
    args = parser.parse_args()

    if args.once:
        fired = run_once()
        sys.exit(0 if fired >= 0 else 1)
    else:
        run_loop(args.interval)
