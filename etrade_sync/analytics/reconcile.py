from decimal import Decimal

from etrade_sync.db import get_connection

TOLERANCE = Decimal("0.01")  # allow rounding differences up to 0.01 shares


def reconcile():
    """
    Compare ledger reconstructed positions to latest API positions snapshot.
    Writes results to reconciliation_log. Returns summary dict.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:

            # Ledger: net quantity per (account, symbol)
            cur.execute("""
                SELECT account_id_key, symbol, SUM(quantity) AS ledger_qty
                FROM ledger
                WHERE event_type IN ('buy', 'sell', 'split', 'redemption')
                  AND symbol IS NOT NULL
                GROUP BY account_id_key, symbol
            """)
            ledger_rows = {(r[0], r[1]): r[2] for r in cur.fetchall()}

            # API: latest positions snapshot
            cur.execute("""
                SELECT account_id_key, symbol, quantity AS api_qty
                FROM positions
                WHERE fetched_at = (SELECT MAX(fetched_at) FROM positions)
                  AND symbol IS NOT NULL
            """)
            api_rows = {(r[0], r[1]): r[2] for r in cur.fetchall()}

            # Merge all keys
            all_keys = set(ledger_rows) | set(api_rows)
            results = []
            counts = {"match": 0, "discrepancy": 0, "ledger_only": 0, "api_only": 0}

            for key in sorted(all_keys):
                acct, sym = key
                ledger_qty = ledger_rows.get(key)
                api_qty = api_rows.get(key)

                if ledger_qty is not None and api_qty is not None:
                    delta = ledger_qty - api_qty
                    if abs(delta) <= TOLERANCE:
                        status = "match"
                        note = None
                    else:
                        status = "discrepancy"
                        # Likely cause: sells/transfers before the 2-year history window
                        note = (
                            "Ledger qty exceeds API qty — sells likely predate "
                            "the 2-year transaction history window."
                            if delta > 0
                            else "API qty exceeds ledger qty — possible missing buys or transfers."
                        )
                elif ledger_qty is not None:
                    # In ledger but not in current positions — fully sold
                    delta = ledger_qty
                    status = "ledger_only"
                    note = "Position closed — present in ledger history but not in current API positions."
                else:
                    # In positions but no ledger activity — may be transfers or pre-history holdings
                    delta = -api_qty
                    status = "api_only"
                    note = "No ledger activity found — position may have been acquired before history window."

                counts[status] += 1
                results.append((acct, sym, ledger_qty, api_qty, delta, status, note))

            # Write to reconciliation_log
            cur.execute("DELETE FROM reconciliation_log WHERE run_at = (SELECT MAX(run_at) FROM reconciliation_log)")
            cur.executemany(
                """
                INSERT INTO reconciliation_log
                    (account_id_key, symbol, ledger_qty, api_qty, delta, status, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                results,
            )

    total = len(results)
    print(f"  reconcile: {total} positions checked")
    print(f"    match:        {counts['match']}")
    print(f"    discrepancy:  {counts['discrepancy']}")
    print(f"    ledger_only:  {counts['ledger_only']}")
    print(f"    api_only:     {counts['api_only']}")

    if counts["discrepancy"] > 0 or counts["api_only"] > 0:
        print("  NOTE: discrepancies are expected for positions held before the 2-year history window.")

    return counts
