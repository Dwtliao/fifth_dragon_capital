from datetime import datetime, timezone

_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def alert_distance(price: float | None, threshold: float) -> float:
    """Normalized distance from current price to an alert's threshold. Missing price sorts last."""
    if price is None or price == 0:
        return float("inf")
    return abs(price - threshold) / price


def sort_active_alerts(alerts: list[dict], prices: dict[str, float | None]) -> list[dict]:
    """Sorts by tier asc, distance-to-trigger asc, last_fired_at desc (missing = oldest)."""
    def key(a):
        distance = alert_distance(prices.get(a["ticker"]), a["threshold"])
        last_fired = a.get("last_fired_at") or _MIN_DT
        return (a["tier"], distance, -last_fired.timestamp())
    return sorted(alerts, key=key)


def sort_archived_alerts(alerts: list[dict]) -> list[dict]:
    """Sorts by archived_at desc, then last_fired_at desc (missing = oldest)."""
    def key(a):
        archived_at = a.get("archived_at") or _MIN_DT
        last_fired = a.get("last_fired_at") or _MIN_DT
        return (-archived_at.timestamp(), -last_fired.timestamp())
    return sorted(alerts, key=key)
