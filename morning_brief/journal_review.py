"""Durable journal review. Extraction stages evidence; only explicit approval activates."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
import sys

from alerts.lifecycle import LOCK_NAMESPACE, load_alerts, valid_price
from alerts.management import fingerprint, _audit, _manual_values, _audit_json
from etrade_sync.db import get_connection

VERSION = 2


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _rows(cur):
    names = [d.name for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def item_token(item):
    return _hash({k: item.get(k) for k in ('id','revision','status','kind','ticker',
        'condition','threshold','timeframe','evidence','validation_error','needs_clarification')})


def normalize_extraction(extracted, text, file_path):
    """Conservative evidence validation; old extraction shapes become blocked ideas."""
    if not isinstance(extracted, dict):
        raise ValueError('Journal extraction must be an object')
    for field in ('positions', 'watch_levels', 'price_alerts'):
        values = extracted.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise ValueError(f'Journal {field} must be an array of objects; nothing staged')
    result = {}
    dates = re.findall(r'\d{4}-\d{2}-\d{2}', str(file_path))
    try:
        source_date = date.fromisoformat(dates[-1]) if dates else None
    except ValueError:
        source_date = None
    compact = lambda value: re.sub(r'\s+', ' ', str(value)).strip()
    aliases = {'GC=F': r'\b(gold|GC)\b', 'SI=F': r'\b(silver|SI)\b',
               'CL=F': r'\b(crude|WTI|CL|oil)\b', '^VIX': r'\bVIX\b',
               'DX-Y.NYB': r'\b(DXY|dollar index)\b',
               'NQ=F': r'\b(NQ|Nasdaq futures)\b', 'ES=F': r'\b(ES|S&P futures)\b',
               'RTY=F': r'\b(RTY|Russell futures)\b'}
    def add(raw, kind, condition=None, level=None):
        ticker = str(raw.get('ticker') or '').strip().upper()
        if not ticker:
            return
        evidence = str(raw.get('evidence') or '').strip()
        timeframe = str(raw.get('timeframe') or 'unspecified').strip().lower()
        label = str(raw.get('label') or raw.get('note') or '')
        error = None
        if not re.fullmatch(r'[A-Z0-9^][A-Z0-9.^=\-]{0,31}', ticker):
            error = 'Invalid or ambiguous instrument'
        elif not evidence or compact(evidence) not in compact(text):
            error = 'Exact journal evidence is missing or does not match'
        elif not re.search(aliases.get(ticker, r'(?<![A-Z0-9])'+re.escape(ticker)+r'(?![A-Z0-9])'), evidence, re.I):
            error = 'Evidence does not identify this instrument; do not substitute index/futures/ETF'
        elif kind != 'context' and (condition not in ('above','below') or not valid_price(level)):
            error = 'Condition or finite positive level missing'
        elif kind != 'context' and not re.search(
                r'(?<![\d.])'+re.escape(format(Decimal(str(level)).normalize(),'f'))+
                (r'0*' if '.' in format(Decimal(str(level)).normalize(),'f') else r'(?:\.0+)?')+r'(?!\d|\.\d)',
                evidence.replace(',', '')):
            error = 'Proposed level is not stated in the evidence'
        # Headings like "Alerts" are analysis, not a notification instruction.
        if kind == 'request' and not re.search(r'\b(alert me|notify me|set (?:an? |the )?alert|create (?:an? |the )?alert)\b', evidence, re.I):
            kind = 'watch'
        temporal = re.search(r'\b(sustained|daily close|weekly close|weekly closing|daily closing|close below|close above)\b', evidence, re.I)
        if temporal:
            timeframe = temporal.group(0).lower()
        ranges = re.search(r'\d[\d,.]*\s*[–-]\s*\$?\d', evidence)
        clarify = kind != 'context' and (timeframe not in ('intraday','price_crossing') or bool(ranges))
        status = 'blocked' if error else 'context' if kind == 'context' else 'idea' if kind == 'watch' else 'pending'
        normalized = str(Decimal(str(level)).normalize()) if valid_price(level) else str(level)
        # Request vs watch are one trading idea; stop intent stays separate.
        key = _hash((ticker, 'stop' if kind == 'stop' else 'context' if kind == 'context' else 'watch', condition, normalized, timeframe))
        candidate = dict(idea_key=key, kind=kind, ticker=ticker, condition=condition,
            threshold=float(level) if valid_price(level) else None, timeframe=timeframe,
            label=label, evidence=evidence, file_path=str(file_path), source_date=source_date,
            validation_error=error, needs_clarification=clarify, status=status)
        if key not in result or status == 'pending' and result[key]['status'] == 'idea':
            result[key] = candidate
    for raw in extracted.get('positions') or []:
        add(raw, 'stop' if raw.get('stop') is not None else 'context', 'below' if raw.get('stop') is not None else None, raw.get('stop'))
    for raw in extracted.get('watch_levels') or []:
        for field, condition in (('support','below'),('resistance','above'),('alert_above','above')):
            if raw.get(field) is not None:
                add(raw, 'watch', condition, raw[field])
    for raw in extracted.get('price_alerts') or []:
        add(raw, 'request', raw.get('condition'), raw.get('threshold'))
    commentary = extracted.get('commentary')
    if commentary is None:
        commentary = []
    if not isinstance(commentary, list):
        print('WARNING: ignored malformed optional commentary (expected an array of objects).', file=sys.stderr)
        commentary = []
    malformed = sum(not isinstance(raw, dict) for raw in commentary)
    if malformed:
        print(f'WARNING: ignored {malformed} malformed optional commentary item(s); actionable ideas preserved.', file=sys.stderr)
    for raw in commentary:
        if not isinstance(raw, dict):
            continue
        add(raw, 'context')
    return list(result.values())


def stage_extraction(extracted, text, file_path):
    items = normalize_extraction(extracted, text, file_path)
    counts = dict(extracted=len(items), pending=0, ideas=0, blocked=0, context=0,
                  activated=0, replacements=0, repeated=0, positions=0, watch=0, alerts=0)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for item in items:
                cur.execute('SELECT * FROM journal_review_items WHERE idea_key=%s FOR UPDATE', (item['idea_key'],))
                existing = _rows(cur)
                if existing:
                    current = existing[0]
                    counts['repeated'] += 1
                    # Never revive rejection/acceptance or silently renew notification intent.
                    if current['status'] in ('accepted','rejected'):
                        cur.execute('UPDATE journal_review_items SET last_seen_at=NOW() WHERE id=%s', (current['id'],))
                    else:
                        status = 'pending' if current['status'] == 'pending' and not item['validation_error'] else item['status']
                        cur.execute('''UPDATE journal_review_items SET label=%s,evidence=%s,file_path=%s,
                            source_date=%s,validation_error=%s,needs_clarification=%s,status=%s,
                            revision=revision+1,updated_at=NOW(),last_seen_at=NOW() WHERE id=%s''',
                            (item['label'],item['evidence'],item['file_path'],item['source_date'],
                             item['validation_error'],item['needs_clarification'],status,current['id']))
                    item_id = current['id']
                else:
                    fields = tuple(item)
                    cur.execute('INSERT INTO journal_review_items ('+','.join(fields)+') VALUES ('+
                                ','.join(['%s']*len(fields))+') RETURNING id', tuple(item.values()))
                    item_id = cur.fetchone()[0]
                cur.execute('''INSERT INTO journal_review_evidence (item_id,file_path,content_hash,evidence,source_date)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (item_id,str(file_path),hashlib.sha256(text.encode()).hexdigest(),item['evidence'],item['source_date']))
                cur.execute('SELECT status FROM journal_review_items WHERE id=%s', (item_id,))
                status = cur.fetchone()[0]
                bucket = {'pending':'pending','idea':'ideas','blocked':'blocked','context':'context'}.get(status)
                if bucket:
                    counts[bucket] += 1
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_items():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM journal_review_items ORDER BY ticker,id DESC')
            return _rows(cur)
    finally:
        conn.close()


def holding_snapshot(ticker):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT SUM(quantity),MAX(fetched_at) FROM positions WHERE symbol=%s
                AND security_type='EQ' AND quantity>0 AND fetched_at>=NOW()-INTERVAL '4 days'
                AND (account_id_key,fetched_at) IN
                (SELECT account_id_key,MAX(fetched_at) FROM positions GROUP BY account_id_key)''',(ticker,))
            quantity,as_of = cur.fetchone()
            return dict(quantity=quantity or 0,as_of=as_of)
    finally:
        conn.close()


def ensure_review_ready():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('journal_review_items')")
            if cur.fetchone()[0] is None:
                raise RuntimeError('Apply python -m alerts.migrate --journal-review before syncing journals')
    finally:
        conn.close()


def journal_review_brief():
    from html import escape
    items = list_items()
    pending = [i for i in items if i['status']=='pending']
    ideas = [i for i in items if i['status']=='idea']
    blocked = sum(i['status']=='blocked' for i in items)
    lines = ['## Journal ideas — not active alerts\n',
        f"**{len(pending)} pending approval** · {len(ideas)} watch ideas · {blocked} needing correction. "
        'Review in P10 → Journal Review. Journal sync never activates these notifications.\n']
    for item in sorted(ideas,key=lambda i:i['last_seen_at'],reverse=True)[:8]:
        clean = lambda s: escape(str(s)).replace('|','/').replace('\n',' ').replace('*','\\*').replace('[','\\[').replace(']','\\]')
        lines.append(f"- **{clean(item['ticker'])}**: {clean(item['condition'])} {clean(item['threshold'])} "
                     f"· {clean(item['timeframe'])} · journal date {item['source_date'] or 'unknown'} · idea #{item['id']}\n")
    if len(ideas)>8:
        lines.append(f'_Showing 8 of {len(ideas)} watch ideas; remaining ideas are in P10 Journal Review._\n')
    return '\n'.join(lines)+'\n---\n'


def _decision(cur, item, action, details):
    cur.execute('INSERT INTO journal_review_decisions (item_id,action,details) VALUES (%s,%s,%s::jsonb)',
                (item['id'],action,json.dumps(_audit_json(details),default=str,allow_nan=False)))


def preview_selection(items, options=None):
    """Caller explicitly chooses add/replace/keep-existing and optional clarification."""
    options = options or {}
    conn = get_connection()
    try:
        current = {a['id']: a for a in load_alerts(conn)}
        result = []
        with conn.cursor() as cur:
            for item in items:
                opt = dict(options.get(item['id']) or {})
                mode = opt.get('mode','stop' if item['kind']=='stop' else 'add')
                target = current.get(opt.get('target_id'))
                if mode == 'stop':
                    key = f"position:{item['ticker']}:stop"
                    matches = [a for a in current.values() if a['source']=='key_levels_watch' and a.get('source_key')==key]
                    target = max(matches, key=lambda a:a['id']) if matches else None
                    cur.execute("SELECT stop FROM key_levels WHERE section='positions' AND ticker=%s", (item['ticker'],))
                    row = cur.fetchone()
                    opt['old_stop'] = str(row[0]) if row else None
                result.append(dict(id=item['id'],token=item_token(item),ticker=item['ticker'],
                    condition=item['condition'],threshold=float(item['threshold']) if item['threshold'] is not None else None,
                    mode=mode,target_id=target['id'] if target else None,
                    target_token=fingerprint(target) if target else None,
                    current_level=target['threshold'] if target else None,
                    current_status=('Paused' if target.get('paused') else 'Snoozed' if target.get('snoozed_until') else 'Archived' if target.get('archived_at') else 'Existing') if target else None,
                    clarified_price_crossing=bool(opt.get('clarified_price_crossing')),
                    old_stop=opt.get('old_stop')))
        return result
    finally:
        conn.close()


def review_action(preview, action, *, expiry_days=None, acknowledge_market=False):
    """Atomic approval/rejection/preparation, with shared poll/edit/purge locks."""
    if action not in ('approve','reject','prepare') or not preview or len({p['id'] for p in preview}) != len(preview):
        raise ValueError('Choose distinct review items and a supported action')
    if action == 'approve' and (not acknowledge_market or (expiry_days is not None and not 1 <= expiry_days <= 3650)):
        raise ValueError('Acknowledge quote/notification timing')
    conn = get_connection()
    try:
        plans = sorted(preview,key=lambda p:p['id'])
        if action == 'approve':
            with conn.cursor() as cur:
                # One global phase order everywhere locks are taken (here, apply_bulk,
                # and the compiler): ALL guards, then ALL per-ticker symbols, then ALL
                # row locks. Never interleave per item/ticker — e.g. guard_A, symbol_A,
                # guard_B, symbol_B can deadlock against a batched guard_A, guard_B,
                # symbol_A, symbol_B from another caller (each holds what the other
                # wants next). A strict two-phase batch, done consistently everywhere,
                # cannot form that cycle.
                for p in sorted((p for p in plans if p['mode']=='stop'),key=lambda p:p['ticker']):
                    cur.execute('SELECT pg_advisory_lock(%s,hashtext(%s))',
                                (LOCK_NAMESPACE+2,f"key_levels_watch:{p['ticker']}:below"))
                for ticker in sorted({p['ticker'] for p in plans}):
                    symbol = int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4],'big',signed=True)
                    cur.execute('SELECT pg_advisory_lock(%s,%s)', (LOCK_NAMESPACE+1,symbol))
                for target in sorted({p['target_id'] for p in plans if p.get('target_id')}):
                    cur.execute('SELECT pg_advisory_lock(%s,%s)', (LOCK_NAMESPACE,target))
            conn.commit()
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM journal_review_items WHERE id=ANY(%s) ORDER BY id FOR UPDATE', ([p['id'] for p in plans],))
            items = {r['id']:r for r in _rows(cur)}
            if len(items) != len(plans):
                raise ValueError('A selected proposal disappeared; refresh')
            touched = set()
            for p in plans:
                item = items[p['id']]
                if item_token(item) != p['token'] or item['status'] in ('accepted','rejected','context'):
                    raise ValueError('Proposal changed or already reviewed; refresh and preview again')
                if p['ticker'] != item['ticker'] or p['condition'] != item['condition'] or p['threshold'] != (float(item['threshold']) if item['threshold'] is not None else None):
                    raise ValueError('Preview identity/condition changed; refresh')
                if action == 'reject':
                    cur.execute("UPDATE journal_review_items SET status='rejected',revision=revision+1,updated_at=NOW() WHERE id=%s", (item['id'],))
                    _decision(cur,item,'reject',{'preview':p})
                    continue
                if item['validation_error']:
                    raise ValueError('Evidence/instrument/level validation failed; reject or correct the journal')
                if action == 'prepare':
                    if item['status']!='idea':
                        raise ValueError('Only a watch idea can be prepared')
                    cur.execute("UPDATE journal_review_items SET status='pending',revision=revision+1,updated_at=NOW() WHERE id=%s", (item['id'],))
                    _decision(cur,item,'prepare',{'preview':p})
                    continue
                if item['status']!='pending' or item['needs_clarification'] and not p.get('clarified_price_crossing'):
                    raise ValueError('Prepare the idea and explicitly clarify unsupported timeframe/range first')
                target = None
                if p.get('target_id'):
                    cur.execute('SELECT id FROM price_alerts WHERE id=%s FOR UPDATE', (p['target_id'],))
                    rows = load_alerts(conn,alert_id=p['target_id'])
                    if not rows or fingerprint(rows[0]) != p['target_token']:
                        raise ValueError('Target alert changed, was purged, or controls changed; refresh')
                    target = rows[0]
                mode = p['mode']
                if mode not in ('add','replace','stop','keep_existing'):
                    raise ValueError('Unsupported approval mode')
                if mode=='add' and target is not None:
                    raise ValueError('New-alert approval cannot modify an existing target')
                if item['kind']=='stop' and mode!='stop' or item['kind']!='stop' and mode=='stop':
                    raise ValueError('Stop proposals require the holding-stop workflow')
                if mode in ('replace','keep_existing') and not target:
                    raise ValueError('Choose an existing target explicitly')
                ticker,_,condition,level,_ = _manual_values(item['ticker'],item['label'],item['condition'],float(item['threshold']),None)
                identity_key = ('stop',ticker) if mode=='stop' else ('target',target['id']) if target else ('new',ticker,condition,level)
                if identity_key in touched:
                    raise ValueError('Batch contains conflicting proposals for the same target')
                touched.add(identity_key)
                before = dict(target) if target else None
                if mode=='keep_existing':
                    if (target['ticker'],target['condition'],target['threshold']) != (ticker,condition,level):
                        raise ValueError('Keep existing requires an exact matching condition')
                    alert_id = target['id']
                else:
                    if mode=='replace' and (target['source']!='manual' or target['ticker']!=ticker):
                        raise ValueError('Only an explicitly selected same-symbol manual alert can be replaced')
                    if mode=='stop':
                        cur.execute('''SELECT 1 FROM positions WHERE symbol=%s AND security_type='EQ' AND quantity>0
                            AND fetched_at>=NOW()-INTERVAL '4 days'
                            AND (account_id_key,fetched_at) IN
                            (SELECT account_id_key,MAX(fetched_at) FROM positions GROUP BY account_id_key) LIMIT 1''',(ticker,))
                        if not cur.fetchone():
                            raise ValueError('No verified current holding; a stop cannot be approved')
                        cur.execute("SELECT stop FROM key_levels WHERE section='positions' AND ticker=%s FOR UPDATE", (ticker,))
                        old = cur.fetchone()
                        if (str(old[0]) if old else None) != p.get('old_stop'):
                            raise ValueError('Saved stop changed since preview')
                        key = f'position:{ticker}:stop'
                        cur.execute("SELECT 1 FROM alert_purge_blocks WHERE source='key_levels_watch' AND identity=%s",(key,))
                        if cur.fetchone():
                            raise ValueError('Managed stop was purged; explicit restoration is required separately')
                        cur.execute("SELECT id FROM price_alerts WHERE source='key_levels_watch' AND source_key=%s ORDER BY id DESC LIMIT 1",(key,))
                        live = cur.fetchone()
                        if (live[0] if live else None) != p.get('target_id'):
                            raise ValueError('Managed stop identity changed since preview')
                        cur.execute("INSERT INTO key_levels (section,ticker,stop) VALUES ('positions',%s,%s) ON CONFLICT(section,ticker) DO UPDATE SET stop=EXCLUDED.stop,updated_at=NOW() WHERE key_levels.stop IS DISTINCT FROM EXCLUDED.stop",(ticker,level))
                    expiry = None if mode=='stop' or expiry_days is None else datetime.now(timezone.utc)+timedelta(days=expiry_days)
                    if mode in ('add','replace'):
                        cur.execute('''SELECT id FROM price_alerts WHERE ticker=%s AND condition=%s AND threshold=%s
                            AND enabled AND archived_at IS NULL AND (expires_at IS NULL OR expires_at>NOW()) AND id<>%s''',
                            (ticker,condition,level,target['id'] if target else -1))
                        if cur.fetchone():
                            raise ValueError('Exact alert already exists; choose Keep existing or review it in P11')
                        # Same guard as a purged managed stop: a purged watch/journal alert is not silently recreated.
                        cur.execute('''SELECT 1 FROM alert_purge_blocks WHERE legacy_identity
                            AND ticker=%s AND condition=%s AND threshold=%s''', (ticker,condition,level))
                        if cur.fetchone():
                            raise ValueError('This alert was previously purged; explicit restoration is required separately')
                    label = f'Stop: {ticker} below {level:g}' if mode=='stop' else item['label'] or f'Approved journal: {ticker} {condition} {level:g}'
                    if target:
                        rearm=(target['threshold'],target['condition']) != (level,condition)
                        cur.execute('UPDATE price_alerts SET threshold=%s,condition=%s,label=%s,expires_at=%s,triggered=CASE WHEN %s THEN FALSE ELSE triggered END WHERE id=%s',
                                    (level,condition,label,expiry,rearm,target['id']))
                        alert_id = target['id']
                        # Preserve enabled/archive/controls. Rearm, but do not resume a suppressed alert.
                        if rearm:
                            cur.execute("UPDATE alert_notification_events SET status='cancelled',updated_at=NOW() WHERE alert_id=%s AND status IN ('pending','failed','unknown','not_configured')",(alert_id,))
                    else:
                        source,key,tier,pinned = ('key_levels_watch',f'position:{ticker}:stop',1,False) if mode=='stop' else ('manual',None,2,True)
                        cur.execute('''INSERT INTO price_alerts (ticker,label,condition,threshold,expires_at,source,source_key,tier,pinned)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''', (ticker,label,condition,level,expiry,source,key,tier,pinned))
                        inserted = cur.fetchone()
                        if not inserted:
                            raise ValueError('Managed creation is blocked by purge intent')
                        alert_id = inserted[0]
                    updated = load_alerts(conn,alert_id=alert_id)[0]
                    _audit(cur,updated,'journal_approval',{'proposal_id':item['id'],'before':before,
                           'after':dict(condition=condition,threshold=level,expires_at=expiry),'clarification':p.get('clarified_price_crossing',False)})
                cur.execute("UPDATE journal_review_items SET status='accepted',alert_id=%s,revision=revision+1,updated_at=NOW() WHERE id=%s",(alert_id,item['id']))
                _decision(cur,item,'approve',{'preview':p,'alert_id':alert_id,'before':before,'expiry_days':expiry_days})
        conn.commit()
        return [p['id'] for p in plans]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
