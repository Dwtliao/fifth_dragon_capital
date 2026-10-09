import unittest
from unittest.mock import patch

from morning_brief.fetchers import fetch_positions


class BriefHoldingsTests(unittest.TestCase):
    def test_only_actual_holdings_with_saved_metadata(self):
        config = {'positions': {'AAPL': {'stop': 100, 'note': 'Keep'},
                                'VIXY': {'stop': 21.51}, 'RBL': {}, 'RPI': {}}}
        with patch('morning_brief.fetchers.fetch_positions_from_db', return_value=[
                {'symbol': 'AAPL', 'quantity': 2, 'cost_basis': 150},
                {'symbol': 'RBL', 'quantity': 2}, {'symbol': 'RPI', 'quantity': 2},
                {'symbol': 'BR', 'quantity': 10},
                {'symbol': 'ZERO', 'quantity': 0}]), \
             patch('etrade_sync.market.quotes.get_quotes_safe', return_value=({}, None)), \
             patch('morning_brief.fetchers._fetch_snapshot', return_value=[]) as quotes:
            rows = fetch_positions(config)
        self.assertEqual([r['ticker'] for r in rows], ['AAPL'])
        self.assertEqual(rows[0]['stop'], 100)
        self.assertEqual(rows[0]['note'], 'Keep')
        quotes.assert_called_once_with({'AAPL': 'AAPL'})

    def test_empty_holdings_does_not_fall_back_to_saved_positions(self):
        with patch('morning_brief.fetchers.fetch_positions_from_db', return_value=[]), \
             patch('morning_brief.fetchers._fetch_snapshot') as quotes:
            self.assertEqual(fetch_positions({'positions': {'VIXY': {'stop': 21.51}}}), [])
            quotes.assert_not_called()

    def test_holdings_error_does_not_fall_back(self):
        with patch('morning_brief.fetchers.fetch_positions_from_db', return_value=[{'error': 'offline'}]):
            with self.assertRaisesRegex(RuntimeError, 'holdings unavailable'):
                fetch_positions({'positions': {'VIXY': {}}})
