import io
import os
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/fake")
os.environ.setdefault("ETRADE_CONSUMER_KEY", "test-key")
os.environ.setdefault("ETRADE_CONSUMER_SECRET", "test-secret")

from etrade_sync.__main__ import main


class SyncErrorLoggingTests(unittest.TestCase):
    def run_sync(self, orders_result=None, orders_error=None, ledger_error=None):
        with ExitStack() as stack:
            stack.enter_context(patch("sys.argv", ["etrade_sync", "sync"]))
            stack.enter_context(redirect_stdout(io.StringIO()))
            for target in (
                "etrade_sync.db.create_tables",
                "etrade_sync.sync.accounts.sync_accounts", "etrade_sync.sync.accounts.sync_balances",
                "etrade_sync.sync.positions.sync_positions", "etrade_sync.sync.transactions.sync_transactions",
                "etrade_sync.analytics.realized_pnl.build_realized_pnl",
                "etrade_sync.analytics.prices.seed_prices", "etrade_sync.analytics.views.refresh_views",
                "etrade_sync.analytics.reconcile.reconcile",
            ):
                stack.enter_context(patch(target, return_value=None))
            stack.enter_context(patch("etrade_sync.db.get_connection"))
            stack.enter_context(patch("etrade_sync.__main__._table_counts", return_value={"positions": 3}))
            stack.enter_context(patch("etrade_sync.analytics.sync_log.start_run", return_value=42))
            finish = stack.enter_context(patch("etrade_sync.analytics.sync_log.finish_run"))
            stack.enter_context(patch("etrade_sync.sync.orders.sync_orders", return_value=orders_result,
                                      side_effect=orders_error))
            stack.enter_context(patch("etrade_sync.analytics.ledger.build_ledger", side_effect=ledger_error))
            exit_code = 0
            try:
                main()
            except SystemExit as exc:
                exit_code = exc.code
            return exit_code, finish.call_args

    def test_partial_orders_errors_retain_each_account_and_message(self):
        code, logged = self.run_sync(orders_result={"errors": ["account1: read timeout", "account2: HTTP 500\nHTTP 500 response body: upstream error"]})
        self.assertEqual(code, 1)
        self.assertEqual(logged.args, (42, "failed"))
        self.assertEqual(logged.kwargs["rows_synced"], {"positions": 3})
        self.assertIn("orders: account1: read timeout", logged.kwargs["error_msg"])
        self.assertIn("orders: account2: HTTP 500", logged.kwargs["error_msg"])
        self.assertIn("HTTP 500 response body: upstream error", logged.kwargs["error_msg"])

    def test_exception_and_downstream_failure_details_are_retained(self):
        code, logged = self.run_sync(orders_error=ValueError("bad order payload"),
                                     ledger_error=ValueError("ledger unavailable"))
        self.assertEqual(code, 1)
        self.assertIn("orders: bad order payload", logged.kwargs["error_msg"])
        self.assertIn("ledger: ledger unavailable", logged.kwargs["error_msg"])

    def test_fatal_error_names_the_step(self):
        code, logged = self.run_sync(orders_error=RuntimeError("token rejected"))
        self.assertEqual(code, 1)
        self.assertEqual(logged.kwargs["error_msg"], "orders: token rejected")

    def test_success_has_no_error_details(self):
        code, logged = self.run_sync(orders_result={"errors": []})
        self.assertEqual(code, 0)
        self.assertEqual(logged.args, (42, "success"))
        self.assertIsNone(logged.kwargs["error_msg"])
