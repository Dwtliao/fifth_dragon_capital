"""Step 4 only: verify retirement on a freshly restored, local disposable database.

Requires WATCH_LEVELS_TEST_DATABASE_URL. Refuses production ports/databases and
checkouts containing .env. Does not fetch quotes, call LLMs, poll, or send email.
Writes only the retired schema/data in the isolated restore and a summary JSON.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

ROOT=Path(__file__).resolve().parents[1]
DSN=os.environ.get('WATCH_LEVELS_TEST_DATABASE_URL','')
settings=parse_dsn(DSN) if DSN else {}
if (settings.get('host') != '/private/tmp' or settings.get('port') != '55441'
        or settings.get('dbname') != 'fdc_watch_levels_step4' or (ROOT/'.env').exists()):
    raise SystemExit('Refusing: use the isolated checkout without .env and the dedicated /private/tmp:55441/fdc_watch_levels_step4 restore')
sys.path.insert(0,str(ROOT))
os.environ.update(DATABASE_URL=DSN,ETRADE_CONSUMER_KEY='isolated-test',
                  ETRADE_CONSUMER_SECRET='isolated-test',ETRADE_DEV='true',ETRADE_LIVE_ORDERS='false',
                  TRADING_DIARY='/private/tmp/fdc_watch_levels_empty_diary')


def connect():
    return psycopg2.connect(DSN)


def digest(values):
    return hashlib.sha256('\n'.join(sorted(values)).encode()).hexdigest()


def main():
    c=connect()
    c.autocommit=True
    def query(statement,params=None):
        with c.cursor() as q:
            q.execute(statement,params) if params is not None else q.execute(statement)
            return q.fetchall() if q.description else []
    def snapshot():
        tables=query("SELECT schemaname,tablename FROM pg_tables WHERE schemaname='public' OR schemaname='recovery_alert_journal_20261014_123231' ORDER BY 1,2")
        data={}
        for schema,table in tables:
            if (schema,table)==('public','key_levels'):
                continue
            values=query(sql.SQL('SELECT row_to_json(t)::text FROM {}.{} t').format(sql.Identifier(schema),sql.Identifier(table)))
            data[f'{schema}.{table}']=(len(values),digest([v[0] for v in values]))
        positions=query("SELECT jsonb_build_array(id,section,ticker,stop,note,updated_at)::text FROM key_levels WHERE section='positions'")
        sequences=query("SELECT schemaname,sequencename,last_value FROM pg_sequences WHERE schemaname='public' OR schemaname='recovery_alert_journal_20261014_123231' ORDER BY 1,2")
        return data,(len(positions),digest([r[0] for r in positions])),sequences
    try:
        assert {r[0] for r in query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='key_levels'")} >= {'support','resistance','alert_above'}, 'Use a fresh restore, not an already migrated copy'
        watch_count=query("SELECT count(*) FROM key_levels WHERE section='watch'")[0][0]
        before,positions,sequences=snapshot()
        migration=(ROOT/'data_model/078_retire_saved_watch_levels.sql').read_text()
        for _ in range(2):
            query(migration)
            after,remaining,after_sequences=snapshot()
            assert before==after, 'Unexpected changes outside saved watch levels'
            assert positions==remaining, 'Position IDs/stops/notes/timestamps changed'
            assert sequences==after_sequences, 'Sequence values changed'
            assert query("SELECT count(*) FROM key_levels WHERE section<>'positions'")[0][0]==0
            assert not query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='key_levels' AND column_name IN ('support','resistance','alert_above')")
        # Source refresh is also checked without committing any changes.
        from morning_brief.alert_compiler import refresh_structural_alerts_from_db
        with patch('morning_brief.alert_compiler.get_connection',connect), \
             patch('morning_brief.alert_compiler._held_symbols',return_value=set()), \
             patch('etrade_sync.db.get_connection',connect):
            refresh_structural_alerts_from_db(dry_run=True)
        assert snapshot()==(before,positions,sequences)
        from streamlit.testing.v1 import AppTest
        import streamlit as st
        st.cache_data.clear()
        with patch('dashboard.db.get_connection',connect),patch('etrade_sync.db.get_connection',connect), \
             patch('dashboard.journal_review_ui.quote_snapshot') as quotes, \
             patch('morning_brief.fetchers._fetch_snapshot',side_effect=AssertionError('Unexpected quote request')):
            p10=AppTest.from_file(str(ROOT/'dashboard/pages/P10_Morning_Brief.py'),default_timeout=30).run()
            assert not p10.exception, 'P10 failed on the retired schema'
            assert not any(s.value=='Watch Levels' for s in p10.subheader)
            assert not any(str(n.key).startswith(('w_sup_','w_res_','w_alrt_')) for n in p10.number_input)
            p11=AppTest.from_file(str(ROOT/'dashboard/pages/P11_Alerts.py'),default_timeout=30).run()
            assert not p11.exception, 'P11 failed on the retired schema'
            quotes.assert_not_called()
        assert snapshot()==(before,positions,sequences), 'Read-only UI smoke changed data'
        report=dict(checkpoint='85f8e2e',database='fdc_watch_levels_step4',
                    restored_backup='fifth_dragon_capital_20261015_175416.dump',
                    watch_rows_removed=watch_count,position_rows_preserved=positions[0],
                    other_tables_unchanged=len(before),position_sha256=positions[1],
                    unchanged_table_fingerprints={k:dict(rows=v[0],sha256=v[1]) for k,v in before.items()},
                    migrations_applied=2,sequences_unchanged=True,recovery_schema_unchanged=True,
                    p10_p11_read_only_smoke_passed=True,dry_run_stop_reconciliation_passed=True,
                    production_touched=False)
        destination=Path('/private/tmp/watch_levels_step4_verification.json')
        destination.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k!='unchanged_table_fingerprints'},indent=2))
        print(f'Fingerprint report: {destination}')
    finally:
        c.close()


if __name__=='__main__':
    main()
