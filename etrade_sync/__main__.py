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


def _table_counts(conn):
    counts = {}
    with conn.cursor() as cur:
        for table in ("accounts", "balances", "positions", "transactions",
                      "orders", "order_details", "ledger", "realized_gains",
                      "market_prices"):
            try:
                cur.execute(f"SELECT count(*) FROM {table}")
                counts[table] = cur.fetchone()[0]
            except Exception:
                pass
    return counts


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

    sub.add_parser("build-realized-pnl", help="FIFO cost basis matching — rebuild realized_gains")

    sub.add_parser("seed-symbols", help="Seed dim_symbols with yfinance metadata")
    sub.add_parser("seed-dates", help="Generate dim_dates date spine")
    sub.add_parser("seed-prices", help="Fetch benchmark prices (SPY) from yfinance into market_prices")
    sub.add_parser("reconcile", help="Compare ledger positions to API positions snapshot")
    sub.add_parser("refresh-views", help="Refresh all materialized views")
    sub.add_parser("migrate", help="Drop + recreate all materialized views and re-apply all SQL files")

    log_p = sub.add_parser("log-event", help="Write a pipeline event to sync_log (used by shell scripts)")
    log_p.add_argument("--job", required=True, help="Job name")
    log_p.add_argument("--status", required=True, choices=["token_stale", "failed"], help="Event status")

    sync_p = sub.add_parser("sync", help="Sync data from E*TRADE to Postgres")
    sync_p.add_argument("--account", metavar="ACCOUNT_ID_KEY", help="Sync a single account only")
    sync_p.add_argument(
        "--only",
        choices=["accounts", "balances", "positions", "transactions", "orders"],
        help="Sync only this data type",
    )

    date_group = sync_p.add_mutually_exclusive_group()
    date_group.add_argument("--days", type=int, metavar="N")
    date_group.add_argument("--from-beginning", action="store_true")
    date_group.add_argument("--from", dest="from_date", type=_parse_date, metavar="YYYY-MM-DD")
    sync_p.add_argument("--to", dest="to_date", type=_parse_date, metavar="YYYY-MM-DD")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # ------------------------------------------------------------------ auth
    if args.command == "auth":
        from etrade_sync.auth import run_auth_flow
        run_auth_flow()

    # ------------------------------------------------------------ log-event
    elif args.command == "log-event":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.sync_log import log_token_stale
        create_tables()
        if args.status == "token_stale":
            log_token_stale(args.job)

    # ----------------------------------------------------- build-realized-pnl
    elif args.command == "build-realized-pnl":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.realized_pnl import build_realized_pnl
        from etrade_sync.analytics.sync_log import start_run, finish_run
        create_tables()
        log_id = start_run("build_realized_pnl")
        try:
            count = build_realized_pnl()
            finish_run(log_id, "success", rows_synced={"realized_gains": count})
        except Exception as e:
            finish_run(log_id, "failed", error_msg=str(e))
            raise

    # ---------------------------------------------------------- build-ledger
    elif args.command == "build-ledger":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.ledger import build_ledger
        from etrade_sync.analytics.sync_log import start_run, finish_run
        create_tables()
        log_id = start_run("build_ledger")
        try:
            count = build_ledger(full_rebuild=args.full_rebuild)
            finish_run(log_id, "success", rows_synced={"ledger": count})
        except Exception as e:
            finish_run(log_id, "failed", error_msg=str(e))
            raise

    # ---------------------------------------------------------- seed-symbols
    elif args.command == "seed-symbols":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.symbols import seed_symbols
        from etrade_sync.analytics.sync_log import start_run, finish_run
        create_tables()
        log_id = start_run("seed_symbols")
        try:
            count = seed_symbols()
            finish_run(log_id, "success", rows_synced={"dim_symbols": count})
        except Exception as e:
            finish_run(log_id, "failed", error_msg=str(e))
            raise

    # ------------------------------------------------------------ seed-dates
    elif args.command == "seed-dates":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.dates import seed_dates
        from etrade_sync.analytics.sync_log import start_run, finish_run
        create_tables()
        log_id = start_run("seed_dates")
        try:
            count = seed_dates()
            finish_run(log_id, "success", rows_synced={"dim_dates": count})
        except Exception as e:
            finish_run(log_id, "failed", error_msg=str(e))
            raise

    # ----------------------------------------------------------- seed-prices
    elif args.command == "seed-prices":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.prices import seed_prices
        from etrade_sync.analytics.sync_log import start_run, finish_run
        create_tables()
        log_id = start_run("seed_prices")
        try:
            count = seed_prices()
            finish_run(log_id, "success", rows_synced={"market_prices": count})
        except Exception as e:
            finish_run(log_id, "failed", error_msg=str(e))
            raise

    # ------------------------------------------------------------ reconcile
    elif args.command == "reconcile":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.reconcile import reconcile
        from etrade_sync.analytics.sync_log import start_run, finish_run
        create_tables()
        log_id = start_run("reconcile")
        try:
            counts = reconcile()
            finish_run(log_id, "success", rows_synced=counts)
        except Exception as e:
            finish_run(log_id, "failed", error_msg=str(e))
            raise

    # --------------------------------------------------------- refresh-views
    elif args.command == "refresh-views":
        from etrade_sync.db import create_tables
        from etrade_sync.analytics.views import refresh_views
        create_tables()
        refresh_views()

    # --------------------------------------------------------------- migrate
    elif args.command == "migrate":
        from etrade_sync.db import migrate
        migrate()

    # --------------------------------------------------------------- sync
    elif args.command == "sync":
        from etrade_sync.db import create_tables, get_connection
        from etrade_sync.sync.accounts import sync_accounts, sync_balances
        from etrade_sync.sync.positions import sync_positions
        from etrade_sync.sync.transactions import sync_transactions
        from etrade_sync.sync.orders import sync_orders
        from etrade_sync.analytics.ledger import build_ledger
        from etrade_sync.analytics.views import refresh_views
        from etrade_sync.analytics.sync_log import start_run, finish_run

        job_name = f"sync:{args.only}" if args.only else "sync"
        start = time.time()
        print(f"[{_ts()}] Starting sync{' (--only ' + args.only + ')' if args.only else ''}")

        try:
            create_tables()
        except Exception as e:
            print(f"[{_ts()}] ERROR: Could not connect to Postgres — {e}")
            sys.exit(1)

        log_id = start_run(job_name)

        # Resolve date range
        start_date = None
        end_date = args.to_date or None
        from_beginning = args.from_beginning

        if args.days:
            start_date = date.today() - timedelta(days=args.days)
        elif args.from_date:
            start_date = args.from_date

        if start_date or from_beginning:
            label = "--from-beginning" if from_beginning else f"--from {start_date}"
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
                print(f"[{_ts()}] ERROR: {e}")
                finish_run(log_id, "failed", error_msg=str(e))
                sys.exit(1)
            except Exception as e:
                print(f"[{_ts()}] WARNING: {name} failed — {e}")
                errors.append(name)
            elapsed = time.time() - t0
            print(f"[{_ts()}] {name} done ({elapsed:.1f}s)")

        # Always rebuild ledger and refresh views after a full sync
        if not args.only:
            print(f"[{_ts()}] ledger...")
            try:
                build_ledger()
            except Exception as e:
                print(f"[{_ts()}] WARNING: ledger rebuild failed — {e}")
            print(f"[{_ts()}] ledger done")

            print(f"[{_ts()}] realized pnl...")
            try:
                from etrade_sync.analytics.realized_pnl import build_realized_pnl
                build_realized_pnl()
            except Exception as e:
                print(f"[{_ts()}] WARNING: realized pnl failed — {e}")
            print(f"[{_ts()}] realized pnl done")

            print(f"[{_ts()}] benchmark prices...")
            try:
                from etrade_sync.analytics.prices import seed_prices
                seed_prices()
            except Exception as e:
                print(f"[{_ts()}] WARNING: benchmark price fetch failed — {e}")
            print(f"[{_ts()}] benchmark prices done")

            print(f"[{_ts()}] refreshing views...")
            try:
                refresh_views()
            except Exception as e:
                print(f"[{_ts()}] WARNING: view refresh failed — {e}")
            print(f"[{_ts()}] views done")

            print(f"[{_ts()}] reconciling positions...")
            try:
                from etrade_sync.analytics.reconcile import reconcile
                reconcile()
            except Exception as e:
                print(f"[{_ts()}] WARNING: reconcile failed — {e}")
            print(f"[{_ts()}] reconcile done")

        # Summary + sync_log
        elapsed_total = time.time() - start
        print(f"\n[{_ts()}] Sync complete in {elapsed_total:.1f}s")
        with get_connection() as conn:
            counts = _table_counts(conn)
            for table, n in counts.items():
                print(f"  {table:<15} {n:>6} rows")

        status = "failed" if errors else "success"
        error_msg = f"errors in: {', '.join(errors)}" if errors else None
        finish_run(log_id, status, rows_synced=counts, error_msg=error_msg)

        if errors:
            print(f"\nWarnings: {', '.join(errors)} had errors (see above)")
            sys.exit(1)


if __name__ == "__main__":
    main()
