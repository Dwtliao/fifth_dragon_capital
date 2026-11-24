"""Deterministic market event dates for the P10 Event Dates tab.

Reminders only: no DB, network, alerts or orders. Dates come from published
contract/expiration rules and a US exchange holiday calendar, so they should be
spot-checked against the Cboe options calendar and CME contract calendars.

Known approximation: CME metals/energy/Treasury holidays are treated as identical to
NYSE holidays. They are very close but not guaranteed identical.
"""
import datetime as dt
from functools import lru_cache
from typing import NamedTuple, Optional

from dateutil.easter import easter

MONTH_CODES = "FGHJKMNQUVXZ"


# ── US exchange business-day calendar ────────────────────────────────────────

def _nth_weekday(year, month, weekday, n):
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    d = (dt.date(year + (month == 12), month % 12 + 1, 1)) - dt.timedelta(days=1)
    while d.weekday() != weekday:
        d -= dt.timedelta(days=1)
    return d


def _observed(d, saturday_to_friday=True):
    if d.weekday() == 5:
        return d - dt.timedelta(days=1) if saturday_to_friday else None
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def exchange_holidays(year):
    """{date: name} of full-day US exchange closures in `year`."""
    days = {
        "New Year's Day": _observed(dt.date(year, 1, 1), saturday_to_friday=False),
        "Martin Luther King Jr. Day": _nth_weekday(year, 1, 0, 3),
        "Presidents' Day": _nth_weekday(year, 2, 0, 3),
        "Good Friday": easter(year) - dt.timedelta(days=2),
        "Memorial Day": _last_weekday(year, 5, 0),
        "Independence Day": _observed(dt.date(year, 7, 4)),
        "Labor Day": _nth_weekday(year, 9, 0, 1),
        "Thanksgiving": _nth_weekday(year, 11, 3, 4),
        "Christmas": _observed(dt.date(year, 12, 25)),
    }
    if year >= 2022:
        days["Juneteenth"] = _observed(dt.date(year, 6, 19))
    return {d: name for name, d in days.items() if d is not None}


def is_business_day(d):
    return d.weekday() < 5 and d not in exchange_holidays(d.year)


def shift_business_days(d, n):
    """Move `d` by `n` business days (negative = earlier)."""
    step = 1 if n > 0 else -1
    while n:
        d += dt.timedelta(days=step)
        if is_business_day(d):
            n -= step
    return d


def _on_or_before(d):
    return d if is_business_day(d) else shift_business_days(d, -1)


def _on_or_after(d):
    return d if is_business_day(d) else shift_business_days(d, 1)


def last_business_day(year, month):
    return _on_or_before(dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1))


# ── Cboe options ─────────────────────────────────────────────────────────────

def standard_expiration(year, month):
    """Third Friday, or the prior business day when that Friday is a holiday."""
    return _on_or_before(_nth_weekday(year, month, 4, 3))


def vix_expiration(year, month):
    """30 days before next month's standard expiration, on or before a business day."""
    ny, nm = year + (month == 12), month % 12 + 1
    return _on_or_before(standard_expiration(ny, nm) - dt.timedelta(days=30))


# ── CME futures ──────────────────────────────────────────────────────────────

class Contract(NamedTuple):
    product: str
    symbol: str        # e.g. GCZ26
    year: int
    month: int
    roll_date: dt.date
    roll_label: str
    last_trade: dt.date
    options_expiry: Optional[dt.date] = None   # COMEX gold options only; volume starts moving here


# code -> (name, delivery months, rule)
PRODUCTS = {
    "GC": ("Gold (COMEX)", (2, 4, 6, 8, 10, 12), "metal"),
    "SI": ("Silver (COMEX)", (3, 5, 7, 9, 12), "metal"),
    "CL": ("Crude Oil WTI (NYMEX)", tuple(range(1, 13)), "crude"),
    "ZN": ("10Y T-Note (CBOT)", (3, 6, 9, 12), "treasury"),
    "ZB": ("30Y T-Bond (CBOT)", (3, 6, 9, 12), "treasury"),
}


def _contract_dates(rule, year, month):
    """(roll_date, roll_label, last_trade) for one delivery month."""
    py, pm = year - (month == 1), (month - 2) % 12 + 1
    if rule == "metal":
        # First notice day = last business day of the prior month; last trade = third-last
        # business day of the delivery month.
        return (last_business_day(py, pm), "first notice day",
                shift_business_days(last_business_day(year, month), -2))
    if rule == "crude":
        # Trading ends 3 business days before the 25th of the prior month (or before the
        # business day preceding the 25th when the 25th is not a business day).
        last = shift_business_days(_on_or_before(dt.date(py, pm, 25)), -3)
        return last, "last trade", last
    # Treasury: first position day = 2 business days before the first business day of the
    # delivery month; last trade = 7 business days before the month's last business day.
    return (shift_business_days(_on_or_after(dt.date(year, month, 1)), -2), "first position day",
            shift_business_days(last_business_day(year, month), -7))


def gold_options_expiry(year, month):
    """COMEX gold options (OG) on the `month` delivery contract.

    Rulebook ch. 115: expire at the close four business days before the end of the month
    preceding the option month, moved to the prior business day when that date is a Friday
    or the day before an exchange holiday. Counting the month's last business day as day 1
    reproduces CME's listed Dec 2026 expiry (Nov 24). Spot-check against the CME calendar.
    """
    py, pm = year - (month == 1), (month - 2) % 12 + 1
    d = shift_business_days(last_business_day(py, pm), -3)
    next_day = lambda: d + dt.timedelta(days=1)
    while d.weekday() == 4 or (next_day().weekday() < 5 and not is_business_day(next_day())):
        d = shift_business_days(d, -1)
    return d


def contracts(product, from_year, years=2):
    name, months, rule = PRODUCTS[product]
    out = []
    for year in range(from_year, from_year + years):
        for month in months:
            roll, label, last = _contract_dates(rule, year, month)
            out.append(Contract(product, f"{product}{MONTH_CODES[month - 1]}{year % 100:02d}",
                                year, month, roll, label, last,
                                gold_options_expiry(year, month) if product == "GC" else None))
    return out


def contract_table(today, per_product=4):
    """Unexpired contracts per product with a chart hint.

    Rolling off / Chart this / Next, plus for gold a roll window: from the options expiry day
    until first notice day the front contract is "Roll window" and the next is "Switch to this".
    """
    rows = []
    for product, (name, _, _) in PRODUCTS.items():
        live = [c for c in contracts(product, today.year - 1, 3) if c.last_trade >= today][:per_product]
        chosen = switch_next = False
        for c in live:
            if switch_next:
                status, switch_next, chosen = "Switch to this", False, True
            elif not chosen and c.roll_date >= today:
                if c.options_expiry is not None and c.options_expiry <= today:
                    status, switch_next = "Roll window", True
                else:
                    status, chosen = "Chart this", True
            else:
                status = "Next" if chosen else "Rolling off"
            rows.append({"product": name, "contract": c.symbol, "status": status,
                         "roll_date": c.roll_date, "roll_label": c.roll_label,
                         "last_trade": c.last_trade})
    return rows


# ── Combined upcoming list ───────────────────────────────────────────────────

def upcoming_events(today, days=60):
    """Sorted event dicts (date, category, event, detail) within [today, today+days]."""
    end = today + dt.timedelta(days=days)
    events = []

    def add(d, category, event, detail=""):
        if today <= d <= end:
            events.append({"date": d, "category": category, "event": event, "detail": detail})

    for i in range(7):
        year, month = today.year + (today.month - 1 + i) // 12, (today.month - 1 + i) % 12 + 1
        std = standard_expiration(year, month)
        quarterly = month in (3, 6, 9, 12)
        add(std, "Options",
            "Quarterly expiration (triple witching)" if quarterly else "Monthly options expiration",
            "Stock options, index options and index futures expire" if quarterly
            else "Standard equity, ETF and index options")
        add(shift_business_days(std, -1), "Options", "Last day to trade AM-settled SPX monthlies",
            f"Expires {std:%b %d}")
        vix = vix_expiration(year, month)
        add(shift_business_days(vix, -1), "Options", "Last day to trade VIX options")
        add(vix, "Options", "VIX options expiration")

    for year in (today.year, today.year + 1):
        for d, name in exchange_holidays(year).items():
            add(d, "Holiday", f"US exchanges closed: {name}")

    for product in PRODUCTS:
        for c in contracts(product, today.year - 1, 3):
            if c.roll_date != c.last_trade:
                add(c.roll_date, "Futures", f"{c.symbol} {c.roll_label}",
                    "Volume typically moves to the next contract around here")
            add(c.last_trade, "Futures", f"{c.symbol} last trading day")
            if c.options_expiry:
                add(c.options_expiry, "Options", f"COMEX gold options expiry ({c.symbol})",
                    "Volume typically starts moving to the next contract")

    return sorted(events, key=lambda e: (e["date"], e["category"], e["event"]))
