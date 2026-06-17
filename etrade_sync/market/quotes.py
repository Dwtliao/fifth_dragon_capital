import math
from typing import Optional

from pyetrade.market import ETradeMarket

from etrade_sync.auth import load_tokens
from etrade_sync.config import CONSUMER_KEY, CONSUMER_SECRET, DEV

_BATCH = 25  # E*TRADE hard limit per quote call


def get_quotes(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch real-time quotes for a list of symbols via E*TRADE API.

    Returns {symbol: {last_price, change, change_pct, volume}} for each
    symbol that came back successfully. Missing symbols are omitted — caller
    should treat absence as unavailable rather than an error.

    Raises RuntimeError if tokens are missing (not yet authed).
    Swallows per-batch API errors and returns whatever succeeded.
    """
    token, secret = load_tokens()
    client = ETradeMarket(CONSUMER_KEY, CONSUMER_SECRET, token, secret, dev=DEV)

    result: dict[str, dict] = {}
    batches = math.ceil(len(symbols) / _BATCH)

    for i in range(batches):
        batch = symbols[i * _BATCH : (i + 1) * _BATCH]
        try:
            resp = client.get_quote(batch, detail_flag="intraday", resp_format="json")
            quote_data = (
                resp.get("QuoteResponse", {})
                    .get("QuoteData", [])
            )
            if isinstance(quote_data, dict):
                quote_data = [quote_data]
            for q in quote_data:
                symbol = q.get("Product", {}).get("symbol")
                intraday = q.get("Intraday", {})
                if not symbol or not intraday:
                    continue
                last = intraday.get("lastTrade")
                change = intraday.get("changeClose")
                change_pct = intraday.get("changeClosePercentage")
                volume = intraday.get("totalVolume")
                if last is not None:
                    result[symbol] = {
                        "last_price":  float(last),
                        "change":      float(change)     if change     is not None else None,
                        "change_pct":  float(change_pct) if change_pct is not None else None,
                        "volume":      int(volume)       if volume      is not None else None,
                    }
        except Exception:
            pass  # stale token or API error — omit this batch, caller falls back

    return result


def get_quotes_safe(symbols: list[str]) -> tuple[dict[str, dict], Optional[str]]:
    """
    Like get_quotes() but never raises. Returns (quotes_dict, error_message).
    error_message is None on full success, a string on partial/total failure.
    """
    try:
        quotes = get_quotes(symbols)
        if not quotes:
            return {}, "E*TRADE returned no quote data — tokens may be expired. Run `python -m etrade_sync auth`."
        return quotes, None
    except RuntimeError as e:
        return {}, str(e)
    except Exception as e:
        return {}, f"Quote fetch failed: {e}"
