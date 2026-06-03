from datetime import date, timedelta

from etrade_sync.db import get_connection

# US federal holidays that fall on weekdays (observed dates may shift).
# We enumerate common fixed-date holidays; floating ones (Thanksgiving, etc.)
# are approximated. For full accuracy, swap this for a market-calendars library.
def _us_holidays(year):
    holidays = set()
    # New Year's Day
    holidays.add(date(year, 1, 1))
    # MLK Day: 3rd Monday of January
    holidays.add(_nth_weekday(year, 1, 0, 3))
    # Presidents' Day: 3rd Monday of February
    holidays.add(_nth_weekday(year, 2, 0, 3))
    # Memorial Day: last Monday of May
    holidays.add(_last_weekday(year, 5, 0))
    # Juneteenth
    holidays.add(date(year, 6, 19))
    # Independence Day
    holidays.add(date(year, 7, 4))
    # Labor Day: 1st Monday of September
    holidays.add(_nth_weekday(year, 9, 0, 1))
    # Thanksgiving: 4th Thursday of November
    holidays.add(_nth_weekday(year, 11, 3, 4))
    # Christmas
    holidays.add(date(year, 12, 25))
    # Shift Saturday holidays to Friday, Sunday holidays to Monday
    adjusted = set()
    for h in holidays:
        if h.weekday() == 5:   # Saturday → Friday
            adjusted.add(h - timedelta(days=1))
        elif h.weekday() == 6: # Sunday → Monday
            adjusted.add(h + timedelta(days=1))
        else:
            adjusted.add(h)
    return adjusted


def _nth_weekday(year, month, weekday, n):
    """Return the nth occurrence of weekday (0=Mon) in given month/year."""
    d = date(year, month, 1)
    count = 0
    while True:
        if d.weekday() == weekday:
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)


def _last_weekday(year, month, weekday):
    """Return the last occurrence of weekday in given month/year."""
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    while last.weekday() != weekday:
        last -= timedelta(days=1)
    return last


_UPSERT = """
    INSERT INTO dim_dates
        (date_key, year, quarter, month, iso_week, iso_year, day_of_week, is_weekend, is_trading_day)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (date_key) DO NOTHING
"""


def seed_dates():
    """Generate date spine from earliest transaction through today + 1 year."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(transaction_date)::date FROM transactions")
            row = cur.fetchone()
            start = row[0] if row and row[0] else date.today() - timedelta(days=365 * 2)

            end = date.today() + timedelta(days=365)

            # Pre-compute holidays for all years in range
            holidays = set()
            for yr in range(start.year, end.year + 1):
                holidays |= _us_holidays(yr)

            rows = []
            d = start
            while d <= end:
                is_weekend = d.weekday() >= 5
                is_trading_day = not is_weekend and d not in holidays
                iso_cal = d.isocalendar()
                rows.append((
                    d,
                    d.year,
                    (d.month - 1) // 3 + 1,
                    d.month,
                    iso_cal[1],         # iso_week
                    iso_cal[0],         # iso_year (differs from d.year near Jan 1)
                    d.weekday(),        # 0=Mon … 6=Sun
                    is_weekend,
                    is_trading_day,
                ))
                d += timedelta(days=1)

            cur.executemany(_UPSERT, rows)

    total = len(rows)
    trading = sum(1 for r in rows if r[7])
    print(f"  dim_dates: {total} days ({start} → {end}), {trading} trading days")
    return total
