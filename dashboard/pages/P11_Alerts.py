"""Alert maintenance without market-chart rendering or implicit quote refreshes."""
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pandas as pd
import streamlit as st
from psycopg2.errors import UndefinedTable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from alerts.lifecycle import is_eligible, state, valid_price
from alerts.management import apply_bulk, consolidation_preview, fingerprint, preview_action, save_manual
from dashboard.alerts_workspace import (current_alerts, filter_alerts, quote_snapshot,
                                        selected_alerts, table_records)
from dashboard.db import query
from morning_brief.alert_compiler import find_duplicate_alerts, find_stale_alerts

st.set_page_config(page_title='Alerts — Fifth Dragon Capital', layout='wide')
st.title('Price Alerts')
st.caption('Manage alerts here; P7 keeps the charts. Selection and filtering do not fetch quotes or send email.')

try:
    alerts = current_alerts()
except UndefinedTable:
    st.error('Apply the Task 1 alert-controls migration before using this page: python -m alerts.migrate')
    st.stop()

def notify(message):
    st.session_state['workspace_message'] = message
    st.session_state.pop('workspace_preview', None)
    st.session_state.pop('workspace_consolidation', None)
    st.session_state['workspace_table_epoch'] = st.session_state.get('workspace_table_epoch', 0) + 1
    st.rerun()

if 'workspace_message' in st.session_state:
    st.success(st.session_state.pop('workspace_message'))

a, b, c = st.columns(3)
a.metric('Active', sum(is_eligible(r) for r in alerts))
b.metric('Paused / Snoozed', sum(state(r) in ('Paused', 'Snoozed') for r in alerts))
c.metric('Archived / Expired / Disabled', sum(state(r) in ('Archived', 'Expired', 'Disabled') for r in alerts))

refresh, reload_data = st.columns(2)
if reload_data.button('Refresh alert status', key='workspace_reload'):
    st.rerun()
if refresh.button('Refresh quotes', key='workspace_quotes_refresh'):
    # Explicit invalidation, independent of table/filter interactions.
    quote_snapshot.clear()
    tickers = tuple(sorted({r['ticker'] for r in alerts if state(r) not in ('Archived', 'Expired')}))
    with st.spinner('Fetching quote snapshot…'):
        st.session_state['workspace_quotes'] = quote_snapshot(tickers)

quotes = st.session_state.get('workspace_quotes', {})
if not quotes:
    st.info('Click Refresh quotes to load current prices and distance. Alert controls work without quotes.')
st.caption('Quote timestamps show retrieval time, not exchange trade time. Yahoo quotes may be delayed. Refresh is explicit; snapshots older than 10 minutes are flagged.')

view = st.radio('View', ['All alerts', 'Needs attention', 'History'], horizontal=True, key='workspace_view')
search = st.text_input('Search ticker, label, or ID', key='workspace_search')
f1, f2, f3, f4 = st.columns(4)
sources = f1.multiselect('Source', ['manual', 'key_levels_watch', 'journal_sync'], key='workspace_sources')
states = f2.multiselect('Status', ['Armed', 'Condition met', 'Paused', 'Snoozed', 'Archived', 'Expired', 'Disabled'], key='workspace_states')
condition = f3.selectbox('Condition', ['Any', 'above', 'below'], key='workspace_condition')
trigger = f4.selectbox('Last poll condition', ['Any', 'Condition met', 'Armed'], key='workspace_trigger')
nearby_col, distance_col = st.columns(2)
nearby = nearby_col.number_input('Nearby threshold (%)', min_value=0.1, max_value=25.0, value=2.0, step=0.5, key='workspace_nearby')
minimum_distance = distance_col.number_input('Minimum distance (%) — 0 shows all', min_value=0.0, value=0.0, step=10.0, key='workspace_minimum_distance')
rows = filter_alerts(alerts, quotes, view=view, search=search, sources=sources,
                     states=states, condition=condition, trigger=trigger, nearby_percent=nearby, minimum_distance=minimum_distance)
context = repr((view, search, sources, states, condition, trigger, nearby, minimum_distance, [r['id'] for r in rows],
                st.session_state.get('workspace_table_epoch', 0)))
table_key = 'alerts_table_' + hashlib.sha256(context.encode()).hexdigest()[:16]
if st.session_state.get('workspace_context') != table_key:
    st.session_state.pop('workspace_preview', None)
    st.session_state['workspace_context'] = table_key

st.caption(f'{len(rows)} of {len(alerts)} alerts shown. Select row checkboxes for details or bulk actions. Selection resets when filters or displayed IDs change; hidden rows are never included.')
selected = []
if rows:
    event = st.dataframe(pd.DataFrame(table_records(rows, quotes, nearby)),
        key=table_key, on_select='rerun', selection_mode='multi-row', hide_index=True,
        width='stretch', column_config={
            'Price': st.column_config.NumberColumn(format='%.4f'),
            'Level': st.column_config.NumberColumn(format='%.4f'),
            'Distance %': st.column_config.NumberColumn(format='%.2f', help='Absolute level-to-price gap divided by current price × 100. Can exceed 100%; check old levels, splits, or units.'),
            'Source': st.column_config.TextColumn(help='manual: operator-owned; key_levels_watch: structural position/watch level; journal_sync: journal-derived level'),
            'Last fired': st.column_config.DatetimeColumn(help='Legacy timestamps may predate delivery tracking. New timestamps reflect successful email delivery.'),
        })
    selected = selected_alerts(rows, event.selection.rows)
else:
    st.info('No alerts match these filters.')

if selected:
    st.subheader(f'Selected alerts ({len(selected)})')
    st.caption('Exact IDs: ' + ', '.join(f"#{r['id']} {r['ticker']}" for r in selected))
    purge_ready = query("SELECT to_regclass('alert_purge_blocks') IS NOT NULL AS ready")[0]['ready']
    actions = ['pause', 'snooze', 'resume', 'archive', 'rearm'] + (['purge'] if purge_ready else [])
    if not purge_ready:
        st.caption('Purge requires the targeted migration: python -m alerts.migrate --purge')
    action = st.selectbox('Action', actions,
                          format_func=lambda a: 'Purge permanently — any source' if a == 'purge' else a.capitalize(), key='workspace_action')
    hours = st.selectbox('Snooze duration (hours)', [1, 4, 24, 72, 168], key='workspace_hours', disabled=action != 'snooze')
    st.caption('Archive is manual-only. Resume/Rearm can notify at the next scheduled poll if the condition is met. These buttons themselves do not send email.')
    if action == 'purge':
        st.warning('Purge permanently removes these alert rows, including archived/expired and managed alerts. Audit snapshots are retained. Managed alerts are blocked from automatic recreation; underlying position/watch/journal levels are not deleted.')
    if st.button('Preview selected action', key='workspace_preview_button'):
        preview = preview_action(selected, action)
        st.session_state['workspace_preview'] = dict(rows=preview, action=action,
            ids=[r['id'] for r in selected], context=table_key, hours=hours,
            until=datetime.now(timezone.utc) + timedelta(hours=hours) if action == 'snooze' else None)
    pending = st.session_state.get('workspace_preview')
    if pending and (pending['ids'] != [r['id'] for r in selected] or pending['action'] != action or
                    pending['hours'] != hours or pending['context'] != table_key):
        st.session_state.pop('workspace_preview', None)
        pending = None
    if pending:
        st.dataframe(pd.DataFrame([{k: v for k, v in r.items() if k != 'fingerprint'} for r in pending['rows']]), hide_index=True, width='stretch')
        if pending['until']:
            st.caption(f"Snoozed until {pending['until']:%Y-%m-%d %H:%M UTC}")
        blocked = any(r['error'] for r in pending['rows'])
        if blocked:
            st.warning('Adjust selection: some rows cannot receive this action. Nothing will be silently skipped.')
        confirmation_key = 'workspace_confirm_' + hashlib.sha256(repr(pending).encode()).hexdigest()[:16]
        confirmed = st.checkbox(f"Confirm {pending['action']} for these {len(pending['rows'])} IDs only", key=confirmation_key)
        confirm, cancel = st.columns(2)
        if confirm.button('Apply confirmed action', key='workspace_apply', disabled=blocked or not confirmed):
            try:
                ids = apply_bulk(pending['rows'], pending['action'], snoozed_until=pending['until'])
            except ValueError as exc:
                st.error(str(exc))
            else:
                notify(f"{pending['action'].capitalize()} saved for alert IDs {ids}.")
        if cancel.button('Cancel preview', key='workspace_cancel'):
            st.session_state.pop('workspace_preview', None)
            st.rerun()

    if len(selected) == 1:
        alert = selected[0]
        st.subheader(f"Details — #{alert['id']} {alert['ticker']}")
        st.caption(f"Source: {alert['source']} · identity: {alert.get('source_key') or 'manual row'} · {state(alert)}")
        if alert.get('delivery_error'):
            st.warning(alert['delivery_error'])
        if alert['source'] != 'manual':
            st.info('This level is source-managed. Edit its position/watch/journal source; Pause/Snooze works here.')
            st.page_link('pages/P10_Morning_Brief.py', label='Open Morning Brief / Key Levels')
        else:
            with st.form(f"manual_edit_{alert['id']}_{fingerprint(alert)[:12]}"):
                ticker = st.text_input('Ticker', value=alert['ticker'])
                label = st.text_input('Label', value=alert.get('label') or '')
                cond = st.selectbox('Condition', ['above', 'below'], index=0 if alert['condition'] == 'above' else 1)
                if not valid_price(alert['threshold']):
                    st.warning('Stored level is invalid. You can purge this alert or save a positive replacement.')
                level = st.number_input('Threshold', min_value=0.0001,
                    value=float(alert['threshold']) if valid_price(alert['threshold']) else 1.0, format='%.4f')
                expires = st.text_input('Expiry (ISO timestamp with timezone; blank = none)',
                                       value=alert['expires_at'].isoformat() if alert.get('expires_at') else '',
                                       placeholder='e.g. 2026-10-15T16:00:00-05:00')
                duplicate = st.checkbox('Allow an intentional exact duplicate')
                st.caption('Changing ticker/condition/threshold rearms this alert; pause/snooze/archive stays unchanged.')
                if st.form_submit_button('Save manual alert'):
                    try:
                        expiry = datetime.fromisoformat(expires.strip()) if expires.strip() else None
                        save_manual(ticker, label, cond, level, expiry,
                                    expected=dict(id=alert['id'], fingerprint=fingerprint(alert)), allow_duplicate=duplicate)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        notify(f"Manual alert #{alert['id']} saved.")
        with st.expander('Action / notification history'):
            st.dataframe(query('SELECT action, details, created_at FROM alert_action_history WHERE alert_id=%s ORDER BY id DESC LIMIT 50', (alert['id'],)), hide_index=True)
            st.dataframe(query('SELECT status, attempts, error, price, updated_at FROM alert_notification_events WHERE alert_id=%s ORDER BY id DESC LIMIT 50', (alert['id'],)), hide_index=True)
else:
    st.caption('Select one row for its detail editor, or several rows for a scoped action preview.')

with st.expander('Add manual alert'):
    with st.form('workspace_add_manual'):
        ticker = st.text_input('Ticker', key='new_ticker')
        label = st.text_input('Label', key='new_label')
        cond = st.selectbox('Condition', ['above', 'below'], key='new_condition')
        level = st.number_input('Threshold', min_value=0.0001, value=1.0, format='%.4f', key='new_threshold')
        expires = st.text_input('Expiry (ISO timestamp with timezone; blank = none)', key='new_expiry', placeholder='e.g. 2026-10-15T16:00:00-05:00')
        duplicate = st.checkbox('Allow an intentional exact duplicate', key='new_duplicate')
        if st.form_submit_button('Create manual alert'):
            try:
                expiry = datetime.fromisoformat(expires.strip()) if expires.strip() else None
                alert_id = save_manual(ticker, label, cond, level, expiry, allow_duplicate=duplicate)
            except ValueError as exc:
                st.error(str(exc))
            else:
                notify(f'Manual alert #{alert_id} created. Refresh quotes if this ticker is new.')

with st.expander('Hygiene reports — load on demand'):
    st.caption('No automatic archive/delete. Only an exact manual/journal + structural pair can be consolidated with confirmation; nearby levels remain review-only. Macro/futures alerts can be valid without portfolio positions.')
    if st.button('Load / refresh hygiene reports', key='workspace_hygiene'):
        active_ids = {r['id'] for r in current_alerts() if is_eligible(r)}
        clusters = []
        for cluster in find_duplicate_alerts():
            eligible_rows = [r for r in cluster['rows'] if r['id'] in active_ids]
            if len(eligible_rows) > 1:
                clusters.append(dict(cluster, rows=eligible_rows))
        st.session_state['workspace_hygiene_reports'] = dict(duplicates=clusters,
            stale=[r for r in find_stale_alerts() if r['id'] in active_ids])
    reports = st.session_state.get('workspace_hygiene_reports')
    if reports:
        st.caption('Report snapshot; reload after changes. Exact duplicates and nearby levels need human review.')
        for cluster in reports['duplicates']:
            st.write(f"{cluster['ticker']} {cluster['condition']}")
            st.dataframe(cluster['rows'], hide_index=True)
            by_id = {r['id']: r for r in alerts}
            pair = [by_id[r['id']] for r in cluster['rows'] if r['id'] in by_id]
            preview = consolidation_preview(pair) if len(pair) == len(cluster['rows']) else None
            if preview and st.button('Preview exact duplicate consolidation', key='duplicate_' + '_'.join(str(r['id']) for r in pair)):
                st.session_state['workspace_consolidation'] = preview
        consolidation = st.session_state.get('workspace_consolidation')
        if consolidation:
            st.dataframe([{k: v for k, v in r.items() if k not in ('fingerprint', 'error')} for r in consolidation], hide_index=True)
            st.caption('The structural alert is retained untouched. The manual/journal duplicate is archived with durable suppression and an audit record.')
            key = 'confirm_consolidation_' + hashlib.sha256(repr(consolidation).encode()).hexdigest()[:16]
            confirmed = st.checkbox('Confirm archive of the displayed duplicate only', key=key)
            if st.button('Consolidate exact duplicate', disabled=not confirmed, key='workspace_consolidate'):
                try:
                    apply_bulk(consolidation, 'consolidate')
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.session_state.pop('workspace_hygiene_reports', None)
                    notify('Exact duplicate archived; structural alert unchanged.')
            if st.button('Cancel consolidation preview', key='workspace_cancel_consolidation'):
                st.session_state.pop('workspace_consolidation', None)
                st.rerun()
        if not reports['duplicates']:
            st.caption('No duplicate clusters.')
        st.dataframe(reports['stale'], hide_index=True)
        st.caption('Manual review cutoff remains 90 days. Select the ID in the main table to Pause or archive a manual alert.')

with st.expander('Poll health and diagnostics'):
    runs = query('SELECT * FROM alert_poll_runs ORDER BY id DESC LIMIT 1')
    if runs:
        st.write(f"Latest poll: {runs[0]['status']} · {runs[0]['started_at']}")
        st.json(runs[0]['summary'])
        if runs[0]['error']:
            st.warning(runs[0]['error'])
    else:
        st.caption('No poll recorded yet.')
    if st.button('Check Alerts — No Emails', key='workspace_check'):
        with st.spinner('Read-only alert check…'):
            result = subprocess.run([sys.executable, '-m', 'alerts.poller', '--dry-run'],
                capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[2]), timeout=180)
        st.session_state['workspace_poll_output'] = result.stdout + result.stderr
    live_confirm = st.checkbox('Allow live polling (can send emails)', key='workspace_live_confirm')
    if st.button('Run live alert poll', key='workspace_live_poll', disabled=not live_confirm):
        with st.spinner('Running live poll…'):
            result = subprocess.run([sys.executable, '-m', 'alerts.poller', '--once'],
                capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[2]), timeout=180)
        st.session_state['workspace_poll_output'] = result.stdout + result.stderr
    if 'workspace_poll_output' in st.session_state:
        st.code(st.session_state['workspace_poll_output'], language=None)
    st.caption('The scheduled five-minute poller remains live independently of this page.')
