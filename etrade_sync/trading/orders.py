"""
Market sell order flow — preview then place.

Gate: ETRADE_LIVE_ORDERS=true must be set in .env.
ETRADE_DEV controls sandbox (true) vs live (false) as usual.

Usage:
    preview = preview_market_sell(account_id_key, symbol, quantity)
    # show preview to user …
    result  = place_market_sell(account_id_key, symbol, quantity,
                                preview["preview_id"], preview["client_order_id"])
"""
import json
import os
import uuid
from datetime import datetime, time as dt_time

from pyetrade.order import ETradeOrder

from etrade_sync.auth import load_tokens
from etrade_sync.config import CONSUMER_KEY, CONSUMER_SECRET, DEV
from etrade_sync.db import get_connection

LIVE_ORDERS = os.environ.get("ETRADE_LIVE_ORDERS", "false").lower() == "true"

_MARKET_OPEN  = dt_time(9, 30)
_MARKET_CLOSE = dt_time(16, 0)


def _check_gates():
    if not LIVE_ORDERS:
        raise RuntimeError(
            "Order placement is disabled. Set ETRADE_LIVE_ORDERS=true in .env to enable."
        )


def _check_market_hours():
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        import pytz
        ZoneInfo = pytz.timezone
    now_et = datetime.now(tz=ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        raise RuntimeError("Market is closed (weekend).")
    if not (_MARKET_OPEN <= now_et.time() < _MARKET_CLOSE):
        raise RuntimeError(
            f"Outside regular session (9:30–16:00 ET). Current ET: {now_et.strftime('%H:%M')}."
        )


def _etrade_client():
    token, secret = load_tokens()
    return ETradeOrder(CONSUMER_KEY, CONSUMER_SECRET, token, secret, dev=DEV)


def _parse_preview(resp: dict) -> dict:
    pr = resp.get("PreviewOrderResponse", {})
    ids = pr.get("PreviewIds", {})
    preview_id = ids.get("previewId")

    order = pr.get("Order", {})
    est_commission = order.get("estimatedCommission")
    est_total      = order.get("estimatedTotalAmount")

    # Broker messages (warnings, disclosures)
    msg_block = pr.get("messageList", {})
    msgs = msg_block.get("Message", [])
    if isinstance(msgs, dict):
        msgs = [msgs]
    messages = [m.get("description", "") for m in msgs if m.get("description")]

    return {
        "preview_id":            preview_id,
        "estimated_commission":  float(est_commission) if est_commission is not None else None,
        "estimated_total":       float(est_total)      if est_total      is not None else None,
        "messages":              messages,
        "raw":                   resp,
    }


def preview_market_sell(account_id_key: str, symbol: str, quantity: int) -> dict:
    """
    Preview a market sell. Returns estimated total, commission, broker messages,
    preview_id, and client_order_id. Raises RuntimeError on any failure.
    """
    _check_gates()
    _check_market_hours()

    client          = _etrade_client()
    client_order_id = uuid.uuid4().hex[:20]

    try:
        resp = client.preview_equity_order(
            accountIdKey=account_id_key,
            symbol=symbol,
            orderAction="SELL",
            clientOrderId=client_order_id,
            priceType="MARKET",
            quantity=quantity,
            orderTerm="GOOD_FOR_DAY",
            marketSession="REGULAR",
        )
    except Exception as e:
        _log_audit(account_id_key, symbol, quantity, client_order_id,
                   status="failed", error_msg=str(e))
        raise RuntimeError(f"Preview failed: {e}") from e

    parsed = _parse_preview(resp)
    parsed["client_order_id"] = client_order_id

    _log_audit(
        account_id_key, symbol, quantity, client_order_id,
        preview_id=parsed["preview_id"],
        estimated_total=parsed["estimated_total"],
        estimated_commission=parsed["estimated_commission"],
        preview_response=resp,
        status="previewed",
    )
    return parsed


def place_market_sell(account_id_key: str, symbol: str, quantity: int,
                      preview_id: str, client_order_id: str) -> dict:
    """
    Place a previewed sell. Must pass the preview_id and client_order_id from
    preview_market_sell() — passing previewId bypasses pyetrade's auto-preview
    so we don't double-submit.
    """
    _check_gates()
    _check_market_hours()

    client = _etrade_client()

    try:
        resp = client.place_equity_order(
            accountIdKey=account_id_key,
            symbol=symbol,
            orderAction="SELL",
            clientOrderId=client_order_id,
            priceType="MARKET",
            quantity=quantity,
            orderTerm="GOOD_FOR_DAY",
            marketSession="REGULAR",
            previewId=preview_id,
        )
    except Exception as e:
        _log_audit(account_id_key, symbol, quantity, client_order_id,
                   status="failed", error_msg=str(e))
        raise RuntimeError(f"Order placement failed: {e}") from e

    pr = resp.get("PlaceOrderResponse", {})
    order_ids = pr.get("OrderIds", {})
    order_id  = order_ids.get("orderId")

    _log_audit(account_id_key, symbol, quantity, client_order_id,
               place_response=resp, status="placed")

    return {"order_id": order_id, "raw": resp}


def _log_audit(account_id_key, symbol, quantity, client_order_id, *,
               preview_id=None, estimated_total=None, estimated_commission=None,
               preview_response=None, place_response=None,
               status="previewed", error_msg=None):
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO sell_order_audit
                        (account_id_key, symbol, quantity, client_order_id,
                         preview_id, estimated_total, estimated_commission,
                         preview_response, place_response, status, error_msg)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (client_order_id) DO UPDATE SET
                        preview_id           = COALESCE(EXCLUDED.preview_id, sell_order_audit.preview_id),
                        estimated_total      = COALESCE(EXCLUDED.estimated_total, sell_order_audit.estimated_total),
                        estimated_commission = COALESCE(EXCLUDED.estimated_commission, sell_order_audit.estimated_commission),
                        preview_response     = COALESCE(EXCLUDED.preview_response, sell_order_audit.preview_response),
                        place_response       = COALESCE(EXCLUDED.place_response, sell_order_audit.place_response),
                        status               = EXCLUDED.status,
                        error_msg            = EXCLUDED.error_msg
                """, (
                    account_id_key, symbol, quantity, client_order_id,
                    preview_id, estimated_total, estimated_commission,
                    json.dumps(preview_response) if preview_response else None,
                    json.dumps(place_response)   if place_response   else None,
                    status, error_msg,
                ))
    except Exception:
        pass  # audit failure must never block the order flow
