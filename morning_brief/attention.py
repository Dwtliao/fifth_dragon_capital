"""Read-only alert attention snapshot, kept out of the LLM payload."""
from datetime import datetime, timezone, date
from html import escape

from alerts.lifecycle import load_alerts, is_eligible, state, valid_price
from etrade_sync.db import get_connection
from morning_brief.fetchers import fetch_positions_from_db, _fetch_snapshot


GROUPS = ('Protect holdings', 'Watch opportunities', 'Resolve ambiguity')


def attention_items(alerts, held, quotes, now=None):
    now = now or datetime.now(timezone.utc)
    groups = {name: [] for name in GROUPS}
    for alert in alerts:
        if alert.get('archived_at'):
            continue
        ticker, level = alert['ticker'], alert['threshold']
        quote = quotes.get(ticker, {})
        price, bar_date = quote.get('last'), quote.get('as_of')
        try:
            age = (now.date() - date.fromisoformat(bar_date)).days
            usable = valid_price(price) and 0 <= age <= 4
        except (ValueError, TypeError):
            usable = False
        stop = alert.get('source_key') == f'position:{ticker}:stop' and alert['source'] == 'key_levels_watch'
        owned, eligible = ticker.upper() in held, is_eligible(alert, now)
        met = usable and valid_price(level) and (
            price > level if alert['condition'] == 'above' else price < level)
        distance = abs(price-level)/price*100 if usable and valid_price(level) else None
        reasons = []
        if stop and not owned:
            reasons.append('Old position stop, but no current holding. Decide: re-entry watch, update, or retire; not a buy signal.')
        if not valid_price(level):
            reasons.append('Invalid stored level')
        if alert.get('expires_at') and alert['expires_at'] <= now:
            reasons.append('Expired idea')
        if eligible and not usable:
            reasons.append('Price unavailable or daily bar stale; current condition unknown')
        if distance is not None and distance >= 25:
            reasons.append(f'Level is {distance:.1f}% from price; check intent/units/rolls/splits')
        recorded = alert.get('last_seen_at') or alert.get('created_at')
        if recorded and (now-recorded).days >= 90:
            reasons.append('Recorded level/evidence is at least 90 days old; confirm intent')
        if alert.get('delivery_status') in ('failed', 'unknown', 'not_configured'):
            reasons.append('Notification delivery: ' + alert['delivery_status'])
        if stop and not owned:
            group = GROUPS[2]
        elif owned and eligible and (met or (stop and distance is not None and distance <= 3)):
            group = GROUPS[0]
        elif not owned and eligible and met:
            group = GROUPS[1]
        elif reasons or (eligible and alert.get('triggered') and not met):
            group = GROUPS[2]
        else:
            continue
        condition = 'met at displayed daily bar' if met else 'not met at displayed daily bar' if usable and valid_price(level) else 'unknown'
        groups[group].append(dict(id=alert['id'], ticker=ticker, owned=owned, price=price,
            bar_date=bar_date, level=level, direction=alert['condition'], condition=condition,
            last_poll='met' if alert.get('triggered') else 'not flagged met', status=state(alert, now),
            delivery=alert.get('delivery_status') or 'no recorded delivery outcome',
            last_fired=alert.get('last_fired_at'), reasons=reasons,
            is_stop=stop,
            rank=0 if met else 1 if stop else 2))
    for rows in groups.values():
        rows.sort(key=lambda r: (r['rank'], r['id']))
    return groups


def render_attention(groups, limit=5, generated_at=None):
    def clean(value):
        return escape(str(value)).replace('|', '/').replace('\n', ' ').replace('*', '\\*').replace('[', '\\[').replace(']', '\\]')
    lines = ['## What needs my attention?\n',
        '_Read-only alert snapshot. Yahoo daily bars may be delayed or still forming—not live prices. '
        'Last-poll condition and recorded delivery are separate. Review IDs in P11 Alerts; '
        'position/watch levels are edited in P10 Key Levels. No changes or extra LLM calls._\n']
    if generated_at:
        lines.append(f"_Snapshot retrieved {generated_at:%Y-%m-%d %H:%M:%S UTC}; regenerate Brief only to refresh._\n")
    for group in GROUPS:
        rows = groups[group]
        lines.append(f'### {group} ({len(rows)})\n')
        if not rows:
            lines.append('_No exceptions under these rules._\n')
            continue
        for row in rows[:limit]:
            price = f"{row['price']:,.2f}" if valid_price(row['price']) else 'unavailable'
            level = f"{row['level']:,.2f}" if valid_price(row['level']) else clean(row['level'])
            met = row['condition'] == 'met at displayed daily bar'
            if row['status'] not in ('Armed', 'Condition met'):
                badge = f":gray[⚪ {row['status']} — notifications inactive]"
            elif row['condition'] == 'unknown':
                badge = ':gray[⚪ Current condition unknown]'
            elif row['is_stop'] and not row['owned']:
                badge = ':orange[🟡 Review old position stop]'
            elif row['is_stop'] and row['owned'] and met:
                badge = ':red[🔴 Held stop breached]'
            elif met and not row['owned']:
                badge = ':green[🟢 Watch condition met — not a buy signal]'
            elif met:
                badge = ':orange[🟡 Held alert condition met]'
            elif row['is_stop'] and group == 'Protect holdings':
                badge = ':orange[🟡 Near stop — condition not met]'
            else:
                badge = ':gray[⚪ Condition not met]'
            review = ("  \n  :orange[🟡 Review needed] — " + clean(' '.join(row['reasons']))) if row['reasons'] else ''
            lines.append(f"- **{clean(row['ticker'])} · #{row['id']}** · {'Held' if row['owned'] else 'Not held'}  \n"
                f"  Alert: **{clean(row['direction'])} {level}** · Price snapshot: **{price}** "
                f"(daily bar {clean(row['bar_date'] or 'unknown')})  \n"
                f"  {badge} · {row['condition']}"
                + review + f"  \n  _Lifecycle: {row['status']} · Last-poll flag: {row['last_poll']} · "
                f"Delivery: {row['delivery']}"
                + (f" · Last recorded successful/legacy fire: {clean(row['last_fired'])}" if row['last_fired'] else '')
                + '._\n')
        if len(rows) > limit:
            lines.append(f'_Showing {limit} of {len(rows)}; review remaining alerts in P11._')
        lines.append('')
    return '\n'.join(lines) + '\n---\n'


def attention_summary():
    holdings = fetch_positions_from_db()
    if any('error' in row for row in holdings):
        raise RuntimeError('Holdings unavailable; cannot distinguish holdings from opportunities')
    held = {row['symbol'].upper() for row in holdings if float(row.get('quantity') or 0) > 0}
    conn = get_connection()
    try:
        conn.set_session(readonly=True)
        alerts = load_alerts(conn)
    finally:
        conn.close()
    tickers = {a['ticker']: a['ticker'] for a in alerts if not a.get('archived_at')}
    quotes = {row['ticker']: row for row in _fetch_snapshot(tickers)} if tickers else {}
    now = datetime.now(timezone.utc)
    return render_attention(attention_items(alerts, held, quotes, now), generated_at=now)
