import argparse
import sys
import time
from datetime import datetime, date, timedelta


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"Date must be YYYY-MM-DD, got: {s}")


def main():
    parser = argparse.ArgumentParser(
        prog="python -m etrade_sync",
        description="E*TRADE data pipeline — sync personal financial data to Postgres",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("auth", help="Complete OAuth dance and save tokens")
    ledger_p = sub.add_parser("build-ledger", help="Populate ledger table from transactions")
    ledger_p.add_argument(
        "--full-rebuild", action="store_true",
        help="Truncate ledger (and dependent fact tables) before repopulating"
    )
    sub.add_parser("seed-symbols", help="Seed dim_symbols with yfinance metadata")
    sub.add_parser("seed-dates", help="Generate dim_dates date spine")

    sync_p = sub.add_parser("sync", help="Sync data from E*TRADE to Postgres")
    sync_p.add_argument(
        "--account", metavar="ACCOUNT_ID_KEY", help="Sync a single account only"
    )
    sync_p.add_argument(
        "--only",
        choices=["accounts", "balances", "positions", "transactions", "orders"],
        help="Sync only this data type",
    )

    # Date range flags (apply to transactions and orders only)
    date_group = sync_p.add_mutually_exclusive_group()
    date_group.add_argument(
        "--days", type=int, metavar="N",
        help="Pull last N days of transactions/orders (e.g. --days 30)"
    )
    date_group.add_argument(
        "--from-beginning", action="store_true",
        help="Pull full 2-year history of transactions/orders"
    )
    date_group.add_argument(
        "--from", dest="from_date", type=_parse_date, metavar="YYYY-MM-DD",
        help="Start date for transactions/orders"
    )
    sync_p.add_argument(
        "--to", dest="to_date", type=_parse_date, metavar="YYYY-MM-DD",
        help="End date for transactions/orders (default: today)"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "auth":
        from etrade_sync.auth import run_auth_flow
        run_auth_flow()

    elif args.command == "build-ledger":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.ledger import build_ledger
        create_tables()
        build_ledger(full_rebuild=args.full_rebuild)

    elif args.command == "seed-symbols":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.symbols import seed_symbols
        create_tables()
        seed_symbols()

    elif args.command == "seed-dates":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.dates import seed_dates
        create_tables()
        seed_dates()

    elif args.command == "sync":
        from etrade_sync.db import create_tables, get_connection
        from etrade_sync.sync.accounts import sync_accounts, sync_balances
        from etrade_sync.sync.positions import sync_positions
        from etrade_sync.sync.transactions import sync_transactions
        from etrade_sync.sync.orders import sync_orders

        start = time.time()
        print(f"[{_ts()}] Starting sync{' (--only ' + args.only + ')' if args.only else ''}")

        try:
            create_tables()
        except Exception as e:
            print(f"[{_ts()}] ERROR: Could not connect to Postgres — {e}")
            sys.exit(1)

        # Resolve date range for transactions/orders
        start_date = None
        end_date = args.to_date or None
        from_beginning = args.from_beginning

        if args.days:
            start_date = date.today() - timedelta(days=args.days)
        elif args.from_date:
            start_date = args.from_date

        if start_date or from_beginning:
            label = f"--from-beginning" if from_beginning else f"--from {start_date}"
            if end_date:
                label += f" --to {end_date}"
            print(f"[{_ts()}] Date range: {label} (transactions + orders only)")

        steps = [
            ("accounts",     lambda: sync_accounts(account_filter=args.account, only=args.only)),
            ("balances",     lambda: sync_balances(account_filter=args.account, only=args.only)),
            ("positions",    lambda: sync_positions(account_filter=args.account, only=args.only)),
            ("transactions", lambda: sync_transactions(account_filter=args.account, only=args.only,
                                                       start_date=start_date, end_date=end_date,
                                                       from_beginning=from_beginning)),
            ("orders",       lambda: sync_orders(account_filter=args.account, only=args.only,
                                                 start_date=start_date, end_date=end_date,
                                                 from_beginning=from_beginning)),
        ]

        errors = []
        for name, fn in steps:
            if args.only and args.only != name:
                continue
            t0 = time.time()
            print(f"[{_ts()}] {name}...")
            try:
                fn()
            except RuntimeError as e:
                # Auth errors (expired token, missing token file)
                print(f"[{_ts()}] ERROR: {e}")
                sys.exit(1)
            except Exception as e:
                print(f"[{_ts()}] WARNING: {name} failed — {e}")
                errors.append(name)
            elapsed = time.time() - t0
            print(f"[{_ts()}] {name} done ({elapsed:.1f}s)")

        # Summary
        print(f"\n[{_ts()}] Sync complete in {time.time() - start:.1f}s")
        with get_connection() as conn:
            with conn.cursor() as cur:
                for table in ("accounts", "balances", "positions", "transactions", "orders", "order_details"):
                    cur.execute(f"SELECT count(*) FROM {table}")
                    print(f"  {table:<15} {cur.fetchone()[0]:>6} rows")

        if errors:
            print(f"\nWarnings: {', '.join(errors)} had errors (see above)")
            sys.exit(1)


if __name__ == "__main__":
    main()
