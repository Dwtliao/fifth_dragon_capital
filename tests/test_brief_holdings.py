import unittest
from unittest.mock import patch

from morning_brief.fetchers import BRIEF_ACCOUNT_TYPES, fetch_holdings, fetch_positions
from morning_brief.formatter import render_positions


class BriefHoldingsTests(unittest.TestCase):
    def test_only_actual_holdings_with_saved_metadata(self):
        config = {'positions': {'AAPL': {'stop': 100, 'note': 'Keep'},
                                'VIXY': {'stop': 21.51}, 'RBL': {}, 'RPI': {}}}
        with patch('morning_brief.fetchers.fetch_holdings', return_value=[
                {'symbol': 'AAPL', 'quantity': 2, 'cost_basis': 150, 'accounts': ['Rollover IRA …1234']},
                {'symbol': 'RBL', 'quantity': 2}, {'symbol': 'RPI', 'quantity': 2},
                {'symbol': 'BR', 'quantity': 10},
                {'symbol': 'ZERO', 'quantity': 0}]) as holdings, \
             patch('etrade_sync.market.quotes.get_quotes_safe', return_value=({}, None)), \
             patch('morning_brief.fetchers._fetch_snapshot', return_value=[]) as quotes:
            rows = fetch_positions(config)
        holdings.assert_called_once_with(BRIEF_ACCOUNT_TYPES)
        self.assertEqual([r['ticker'] for r in rows], ['AAPL'])
        self.assertEqual(rows[0]['stop'], 100)
        self.assertEqual(rows[0]['note'], 'Keep')
        self.assertEqual(rows[0]['accounts'], ['Rollover IRA …1234'])
        quotes.assert_called_once_with({'AAPL': 'AAPL'})

    def test_empty_holdings_does_not_fall_back_to_saved_positions(self):
        with patch('morning_brief.fetchers.fetch_holdings', return_value=[]), \
             patch('morning_brief.fetchers._fetch_snapshot') as quotes:
            self.assertEqual(fetch_positions({'positions': {'VIXY': {'stop': 21.51}}}), [])
            quotes.assert_not_called()

    def test_holdings_error_does_not_fall_back(self):
        with patch('morning_brief.fetchers.fetch_holdings', side_effect=Exception('offline')):
            with self.assertRaisesRegex(RuntimeError, 'holdings unavailable'):
                fetch_positions({'positions': {'VIXY': {}}})

    def test_brief_scope_is_rollover_ira(self):
        self.assertEqual(BRIEF_ACCOUNT_TYPES, ('IRA_ROLLOVER',))


class _FakeCursor:
    description = [type('Col', (), {'name': 'symbol'})()]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchall(self):
        return [('PSLV',)]


class _FakeConn:
    def __init__(self):
        self.cur, self.closed = _FakeCursor(), False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class FetchHoldingsQueryTests(unittest.TestCase):
    def _run(self, account_types):
        conn = _FakeConn()
        with patch('etrade_sync.db.get_connection', return_value=conn):
            rows = fetch_holdings(account_types)
        self.assertTrue(conn.closed)
        return rows, conn.cur

    def test_filters_to_known_active_equity_accounts_and_sums_per_symbol(self):
        rows, cur = self._run(('IRA_ROLLOVER',))
        self.assertEqual(rows, [{'symbol': 'PSLV'}])
        for clause in ('JOIN accounts', "a.status = 'ACTIVE'", "u.security_type = 'EQ'", 'GROUP BY u.symbol'):
            self.assertIn(clause, cur.sql)
        self.assertEqual(cur.params, {'types': ['IRA_ROLLOVER']})

    def test_no_account_types_means_all_active_accounts(self):
        _, cur = self._run(None)
        self.assertEqual(cur.params, {'types': None})


class PositionsTitleTests(unittest.TestCase):
    def test_title_names_source_accounts(self):
        text = render_positions([{'label': 'PSLV', 'ticker': 'PSLV', 'price_source': 'etrade',
                                  'accounts': ['Rollover IRA …1234']}])
        self.assertIn('YOUR POSITIONS — Rollover IRA …1234', text.splitlines()[0])

    def test_title_unchanged_without_account_info(self):
        text = render_positions([{'label': 'X', 'ticker': 'X', 'price_source': 'etrade'}])
        self.assertTrue(text.startswith('## 💼  YOUR POSITIONS  _(prices'))


if __name__ == '__main__':
    unittest.main()
