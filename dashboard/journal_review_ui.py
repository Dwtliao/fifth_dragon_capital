"""P10 journal approval UI; no quote fetches or live changes on ordinary reruns."""
from datetime import datetime, timezone
import hashlib
import pandas as pd
import streamlit as st

from alerts.lifecycle import state, valid_price
from dashboard.alerts_workspace import current_alerts, quote_snapshot
from morning_brief.journal_review import list_items, item_token, preview_selection, review_action, holding_snapshot


def render_journal_review():
    st.subheader('Journal Review — ideas are not notifications')
    st.caption('Sync reads/stages evidence only. New alerts and stop changes require approval. '
               'Approval sends no email, but an active met condition may notify at the next poll.')
    try:
        items = list_items()
        alerts = current_alerts()
    except Exception:
        st.info('Apply journal-review migration first: python -m alerts.migrate --journal-review')
        return
    if st.session_state.get('journal_review_flash'):
        st.success(st.session_state.pop('journal_review_flash'))
    counts = {name:sum(i['status']==name for i in items) for name in ('pending','idea','blocked')}
    st.write(f"Pending approval: {counts['pending']} · Watch ideas: {counts['idea']} · Needs correction: {counts['blocked']}")
    view = st.radio('Review view',['Pending approval','Watch ideas','Needs correction','Reviewed / context'],horizontal=True,key='jr_view')
    search = st.text_input('Search journal symbol or idea',key='jr_search').strip().casefold()
    states = {'Pending approval':('pending',),'Watch ideas':('idea',),
              'Needs correction':('blocked',),'Reviewed / context':('accepted','rejected','context')}[view]
    rows = [i for i in items if i['status'] in states and (not search or search in f"{i['ticker']} {i['label']}".casefold())]
    context = hashlib.sha256(repr([(r['id'],item_token(r)) for r in rows]).encode()).hexdigest()[:12]
    if st.session_state.get('jr_context') != context:
        st.session_state.pop('jr_preview',None)
        st.session_state['jr_context'] = context
    if st.button('Refresh review / alert status',key='jr_refresh'):
        st.session_state.pop('jr_preview',None)
        st.rerun()
    if st.button('Refresh review quotes',key='jr_quotes'):
        quote_snapshot.clear()
        with st.spinner('Refreshing selected-view quotes…'):
            st.session_state['jr_quote_snapshot'] = quote_snapshot(tuple(sorted({r['ticker'] for r in rows})))
    quotes = st.session_state.get('jr_quote_snapshot',{})
    selected, options = [], {}
    for ticker in sorted({r['ticker'] for r in rows}):
        with st.expander(f"{ticker} — {sum(r['ticker']==ticker for r in rows)} items",expanded=bool(search)):
            related = [a for a in alerts if a['ticker']==ticker]
            if related:
                st.caption('Existing alerts — unchanged by journal sync:')
                st.dataframe([dict(ID=a['id'],Source=a['source'],Condition=a['condition'],Level=a['threshold'],Status=state(a)) for a in related],hide_index=True)
            for item in [r for r in rows if r['ticker']==ticker]:
                token = item_token(item)[:12]
                prefix = f"jr_{item['id']}_{token}"
                st.markdown(f"**Idea #{item['id']} · {item['kind']} · {item['status']}**")
                st.write(f"{item['condition'] or 'Context'} {item['threshold'] if item['threshold'] is not None else ''} · timeframe: {item['timeframe']}")
                st.caption(f"Journal: {item['file_path'].split('/')[-1]} · evidence date: {item['source_date'] or 'not provided'}")
                if item.get('alert_id'):
                    st.caption(f"Reviewed decision linked to alert #{item['alert_id']}; manage it in P11.")
                st.text(item['evidence'] or 'No validated excerpt')
                if item['validation_error']:
                    st.warning(item['validation_error'])
                if item['source_date'] and (datetime.now(timezone.utc).date()-item['source_date']).days>21:
                    st.warning('Evidence is older than 21 days. Confirm it still expresses your intent; sync time is not evidence freshness.')
                if item['status'] not in ('accepted','rejected','context'):
                    if st.checkbox(f"Select idea #{item['id']}",key=prefix+'_selected'):
                        selected.append(item)
                    opt = {}
                    if item['kind']=='stop':
                        opt['mode']='stop'
                        try:
                            holding = holding_snapshot(ticker)
                            st.caption(f"Verified snapshot quantity: {holding['quantity']}; as of {holding['as_of'] or 'unavailable'}.")
                        except Exception:
                            st.warning('Holdings lookup unavailable; stop approval will be blocked.')
                        st.caption('Holding stop: snapshot must be within 4 calendar days (weekends supported); approval updates only this stop.')
                    else:
                        opt['mode']=st.selectbox('On approval',['add','replace','keep_existing'],
                            format_func=lambda x:{'add':'Create manual alert','replace':'Replace selected manual alert','keep_existing':'Keep exact existing alert — no change'}[x],key=prefix+'_mode')
                        targets = [a for a in related if a['source']=='manual'] if opt['mode']=='replace' else related
                        if opt['mode']!='add':
                            opt['target_id']=st.selectbox('Explicit target alert',[None]+[a['id'] for a in targets],
                                format_func=lambda value:'Choose a target' if value is None else f'Alert #{value}',key=prefix+'_target')
                    if item['needs_clarification']:
                        st.warning('Daily/weekly/sustained/unspecified or range evidence is NOT implemented as a confirmed-close rule.')
                        opt['clarified_price_crossing']=st.checkbox(
                            f"I deliberately choose a simple {item['condition']} {item['threshold']} price crossing instead of the journal timeframe/range",
                            key=prefix+'_clarify')
                    quote = quotes.get(ticker,{})
                    if valid_price(quote.get('price')):
                        st.caption(f"Quote snapshot {quote['price']:g}, retrieved {quote['retrieved_at']:%Y-%m-%d %H:%M UTC}; may be delayed.")
                        if valid_price(item['threshold']) and ((item['condition']=='above' and quote['price']>item['threshold']) or (item['condition']=='below' and quote['price']<item['threshold'])):
                            st.warning('Proposed condition is already met at this quote; approval may lead to notification at the next poll.')
                    else:
                        st.caption('Quote unavailable/not refreshed; do not treat the proposed condition as verified.')
                    options[item['id']]=opt
    if not rows:
        st.info('No items in this view. Sync Latest Journal to stage new evidence; older syncs are re-extracted once with the approval schema.')
    if not selected:
        st.caption('Select specific items to prepare, approve, or reject. Nothing is selected by default.')
        st.session_state.pop('jr_preview',None)
        return
    action = st.selectbox('Selected review action',['approve','reject','prepare'],
        format_func=lambda x:{'approve':'Approve selected candidates','reject':'Reject selected ideas','prepare':'Prepare watch ideas for approval — no alerts'}[x],key='jr_action')
    expiry = st.number_input('New/replaced manual alert expiry (days)',min_value=1,max_value=3650,value=21,key='jr_expiry')
    st.caption('Held stop alerts have no expiry. Preparation and rejection never change live alerts.')
    signature = repr(([(i['id'],item_token(i)) for i in selected],options,action,expiry))
    if st.button('Preview selected review action',key='jr_preview_button'):
        try:
            preview = preview_selection(selected,options)
            st.session_state['jr_preview']=dict(rows=preview,action=action,signature=signature,expiry=expiry)
        except Exception as exc:
            st.error(str(exc))
    pending = st.session_state.get('jr_preview')
    if pending and pending['signature']!=signature:
        st.session_state.pop('jr_preview',None)
        pending=None
    if pending:
        st.dataframe(pd.DataFrame([{k:v for k,v in r.items() if k not in ('token','target_token')} for r in pending['rows']]),hide_index=True)
        key = hashlib.sha256(signature.encode()).hexdigest()[:12]
        confirmed = st.checkbox('Confirm this action for the displayed proposal IDs only',key='jr_confirm_'+key)
        market = st.checkbox('I reviewed evidence and timing: quotes may be missing/delayed; a met condition can notify on the next poll',key='jr_market_'+key) if action=='approve' else True
        if st.button('Apply confirmed review action',key='jr_apply',disabled=not(confirmed and market)):
            try:
                ids = review_action(pending['rows'],action,expiry_days=expiry,acknowledge_market=market)
            except Exception as exc:
                st.error(str(exc))
            else:
                st.session_state.pop('jr_preview',None)
                st.session_state['journal_review_flash']=f'{action.capitalize()} saved for proposal IDs {ids}. No email sent.'
                st.rerun()
        st.button('Cancel review preview',key='jr_cancel',
                  on_click=lambda:st.session_state.pop('jr_preview',None))
