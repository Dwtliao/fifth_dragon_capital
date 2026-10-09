import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/fake")
os.environ.setdefault("ETRADE_CONSUMER_KEY", "test-key")
os.environ.setdefault("ETRADE_CONSUMER_SECRET", "test-secret")

from etrade_sync.sync.orders import _request_error_message, sync_orders, WATERMARK_UPSERT


class OrdersErrorTests(unittest.TestCase):
    def response(self, status, body):
        response = requests.Response()
        response.status_code = status
        response.url = "https://api.etrade.com/v1/accounts/test/orders.json"
        response.encoding = "utf-8"
        response._content = body.encode("utf-8")
        return response

    def test_falsey_404_response_keeps_complete_json_body(self):
        body = '{"Error":{"code":100,"message":"No orders found for the requested dates"}}'
        response = self.response(404, body)
        self.assertFalse(response)
        detail = _request_error_message(requests.HTTPError("404 Not Found"), response)
        self.assertIn("404 Not Found", detail)
        self.assertIn(body, detail)

    def test_server_error_preserves_plain_text_and_long_body(self):
        body = "Upstream failure\n" + "detail " * 1000
        detail = _request_error_message(requests.HTTPError("500 error"), self.response(500, body))
        self.assertIn(body.strip(), detail)

    def test_empty_body_and_transport_error_are_explicit(self):
        self.assertIn("<empty response body>", _request_error_message(
            requests.HTTPError("404 error"), self.response(404, "")))
        self.assertEqual(_request_error_message(requests.Timeout("read timed out"), None), "read timed out")

    def run_orders(self, response):
        with patch("etrade_sync.sync.orders.load_tokens", return_value=("token", "secret")), \
             patch("etrade_sync.sync.orders.ETradeAccounts") as client, \
             patch("etrade_sync.sync.orders._list_accounts", return_value=[{"accountIdKey": "test"}]), \
             patch("etrade_sync.sync.orders.get_connection") as connection, \
             redirect_stdout(io.StringIO()) as output:
            client.return_value.session.get.return_value = response
            cursor = connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = None
            result = sync_orders()
            watermark_updated = any(call.args[0] == WATERMARK_UPSERT for call in cursor.execute.call_args_list)
            return result, output.getvalue(), watermark_updated

    def test_body_reaches_returned_errors_without_advancing_watermark(self):
        body = '{"Error":{"code":100,"message":"No orders found"}}'
        result, output, updated = self.run_orders(self.response(404, body))
        self.assertIn(body, result["errors"][0])
        self.assertIn(body, output)
        self.assertFalse(updated)

    def test_successful_empty_result_is_not_an_error(self):
        result, output, updated = self.run_orders(self.response(204, ""))
        self.assertEqual(result["errors"], [])
        self.assertTrue(updated)
