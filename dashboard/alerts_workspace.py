"""Shared read/presentation helpers; quotes are explicit snapshots, not mutations."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import streamlit as st
import yfinance as yf

from alerts.lifecycle import is_eligible, load_alerts, state, valid_price
from dashboard import db


def current_alerts():
    conn = db.get_connection()
    try:
        return load_alerts(conn)
    finally:
        conn.close()


@st.cache_data(ttl=120, show_spinner=False)
def quote_snapshot(tickers):
    def fetch(ticker):
        price, error = None, None
        try:
            value = yf.Ticker(ticker).fast_info.last_price
            if valid_price(value):
                price = float(value)
            else:
                error = 'Invalid quote'
        except Exception as exc:
            error = f'Quote unavailable ({type(exc).__name__})'
        return ticker, dict(price=price, error=error, retrieved_at=datetime.now(timezone.utc))
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(fetch, tickers))


def distance_percent(alert, quote):
    price = (quote or {}).get('price')
    if not valid_price(price) or not valid_price(alert['threshold']):
        return None
    return abs(float(price) - float(alert['threshold'])) / float(price) * 100


@st.cache_data(ttl=3600, show_spinner=False)
def split_snapshot(tickers):
    """Fetch public split evidence only for outliers, on explicit review request."""
    def fetch(ticker):
        try:
            history = yf.Ticker(ticker).history(period='2y', auto_adjust=False, actions=True)
            if history.empty or 'Stock Splits' not in history:
                return ticker, dict(events=[], error='Split history unavailable')
            events = [dict(date=date.date(), ratio=float(ratio))
                      for date, ratio in history['Stock Splits'].items() if valid_price(ratio)]
            return ticker, dict(events=events, error=None)
        except Exception as exc:
            return ticker, dict(events=[], error=type(exc).__name__)
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(fetch, tickers))


def attention_reasons(alert, quote, nearby_percent=2.0, now=None):
    now = now or datetime.now(timezone.utc)
    reasons = []
    if alert.get('delivery_status') in ('failed', 'unknown', 'not_configured'):
        reasons.append('Delivery ' + alert['delivery_status'])
    if is_eligible(alert, now):
        if alert.get('triggered'):
            reasons.append('Condition met (last poll)')
        if not quote or not valid_price(quote.get('price')):
            reasons.append('Quote missing')
        else:
            price = quote['price']
            if (alert['condition'] == 'above' and price > alert['threshold']) or (alert['condition'] == 'below' and price < alert['threshold']):
                reasons.append('Quote past level')
            distance = distance_percent(alert, quote)
            if distance is not None and distance <= nearby_percent:
                reasons.append('Nearby level')
            if distance is not None and distance >= 100:
                reasons.append('Very distant level — check units/splits')
            if (now - quote['retrieved_at']).total_seconds() > 600:
                reasons.append('Snapshot needs refresh')
        created = alert.get('created_at')
        if alert['source'] == 'manual' and not alert.get('last_fired_at') and created and (now-created).days >= 90:
            reasons.append('Manual level review (90 days)')
    return reasons


def filter_alerts(alerts, quotes, *, view='All alerts', search='', sources=(), states=(),
                  condition='Any', trigger='Any', nearby_percent=2.0, minimum_distance=0.0):
    result = []
    search = search.strip().casefold()
    for alert in alerts:
        status = state(alert)
        if view == 'History' and status not in ('Archived', 'Expired', 'Disabled'):
            continue
        if view == 'Needs attention' and not attention_reasons(alert, quotes.get(alert['ticker']), nearby_percent):
            continue
        if search and search not in f"{alert['ticker']} {alert.get('label') or ''} {alert['id']}".casefold():
            continue
        if sources and alert['source'] not in sources:
            continue
        if states and status not in states:
            continue
        if condition != 'Any' and alert['condition'] != condition:
            continue
        if trigger != 'Any' and bool(alert['triggered']) != (trigger == 'Condition met'):
            continue
        distance = distance_percent(alert, quotes.get(alert['ticker']))
        if minimum_distance > 0 and (distance is None or distance < minimum_distance):
            continue
        result.append(alert)
    # Stable server ordering avoids remapping selected indexes on quote refresh.
    return sorted(result, key=lambda a: a['id'])


def selected_alerts(rows, indices):
    if any(not isinstance(i, int) or i < 0 or i >= len(rows) for i in indices):
        raise ValueError('Selection changed; select the displayed rows again')
    return [rows[i] for i in sorted(set(indices))]


def table_records(rows, quotes, nearby_percent=2.0):
    records = []
    for alert in rows:
        quote = quotes.get(alert['ticker']) or {}
        records.append({
            'ID': alert['id'], 'Ticker': alert['ticker'], 'Label': alert.get('label') or '',
            'Condition': alert['condition'], 'Level': alert['threshold'],
            'Price': quote.get('price'), 'Distance %': distance_percent(alert, quote),
            'Quote retrieved (UTC)': quote['retrieved_at'].strftime('%Y-%m-%d %H:%M:%S UTC') if quote.get('retrieved_at') else None,
            'Source': alert['source'], 'Status': state(alert),
            'Expiry': alert.get('expires_at'), 'Snooze until': alert.get('snoozed_until'),
            'Last fired': alert.get('last_fired_at'),
            'Delivery': alert.get('delivery_status') or 'No recorded outcome',
            'Attention': '; '.join(attention_reasons(alert, quote, nearby_percent)),
        })
    return records
