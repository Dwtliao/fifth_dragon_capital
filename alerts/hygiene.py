"""Deterministic, read-only review rules. Findings are not trading instructions."""
from datetime import datetime, timezone

from alerts.lifecycle import state, valid_price


def review_exceptions(alerts, quotes, split_history=None, *, now=None,
                      distance_limit=25.0, age_days=90, include_archived=False):
    now = now or datetime.now(timezone.utc)
    split_history = split_history or {}
    findings = []
    for alert in alerts:
        status = state(alert, now)
        if status == 'Archived' and not include_archived:
            continue
        reasons, suggestions = [], []
        priority = 3
        level = alert['threshold']
        quote = quotes.get(alert['ticker']) or {}
        price, retrieved = quote.get('price'), quote.get('retrieved_at')
        fresh = retrieved and 0 <= (now - retrieved).total_seconds() <= 600
        distance = None
        if not valid_price(level):
            reasons.append('Invalid stored level')
            suggestions.append('Replace with a finite positive level or purge')
            priority = 1
        if alert.get('expires_at') and alert['expires_at'] <= now:
            reasons.append('Expired idea')
            suggestions.append('Review whether to retire or deliberately renew')
            priority = 1
        # Compiler refreshed_at is not evidence that a trading level was reviewed.
        recorded = alert.get('last_seen_at') if alert['source'] == 'journal_sync' else None
        recorded = recorded or alert.get('created_at')
        age = max(0, (now - recorded).days) if recorded else None
        if age is not None and age >= age_days:
            description = 'Journal evidence age' if alert.get('last_seen_at') and alert['source'] == 'journal_sync' else 'Age since first recorded'
            reasons.append(f'{description}: {age} days (review, not proof of staleness)')
            suggestions.append('Confirm the level still expresses your intent')
        if not valid_price(price) or not fresh:
            reasons.append('Quote missing or retrieval snapshot older than 10 minutes')
            suggestions.append('Refresh market data before judging distance')
            priority = min(priority, 2)
        elif valid_price(level):
            distance = abs(float(level) - float(price)) / float(price) * 100
            if distance >= distance_limit:
                reasons.append(f'Large price distance: {distance:.1f}%')
                suggestions.append('Check old thesis, units, contract rolls, or corporate actions')
                priority = min(priority, 2)
                history = split_history.get(alert['ticker']) or {}
                if history.get('error'):
                    reasons.append('Split history unavailable (no split conclusion)')
                elif recorded:
                    # Combine only split events after the recorded evidence; multiple
                    # splits must be cumulative. Never change or suggest an exact level.
                    factor, dates = 1.0, []
                    for event in sorted(history.get('events', []), key=lambda e: e['date']):
                        if recorded.date() < event['date'] <= now.date() and valid_price(event['ratio']):
                            factor *= float(event['ratio'])
                            dates.append(event['date'].isoformat())
                    if factor != 1 and valid_price(factor):
                        adjusted_distance = abs(float(level) / factor - float(price)) / float(price) * 100
                        if adjusted_distance <= distance_limit and adjusted_distance < distance / 2:
                            reasons.append(f'Possible split mismatch: cumulative ratio {factor:g}, dates {", ".join(dates)}')
                            suggestions.append('Verify broker/corporate-action records before editing; heuristic only')
                            priority = 1
        if reasons:
            findings.append({'Priority': priority, 'ID': alert['id'], 'Ticker': alert['ticker'],
                'Source': alert['source'], 'Status': status, 'Level': level, 'Price': price,
                'Distance %': distance, 'Evidence / recorded date': recorded,
                'Quote retrieved (UTC)': retrieved, 'Reasons': '; '.join(reasons),
                'Suggested review': '; '.join(dict.fromkeys(suggestions))})
    return sorted(findings, key=lambda row: (row['Priority'], row['ID']))
