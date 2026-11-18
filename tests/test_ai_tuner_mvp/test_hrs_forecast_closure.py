"""Funding reference closure is raw evidence integrity, not replay decisions."""
import copy
from datetime import timedelta
import json
import os
import uuid
from datetime import datetime
from decimal import Decimal

import pytest

from ai_tuner.hrs_evidence_export import export_evidence_bundle
from strategies.hrs.equity_evidence import canonical
from strategies.hrs.funding_reference import build_forecasts_ref
from tests.test_ai_tuner_mvp.test_hrs_evidence_export import captured_sources
from tests.test_ai_tuner_mvp.replay.test_hrs_wiring import AT
from pathlib import Path


def observation(at,received=None,account='test'):
    received=received or at-timedelta(seconds=1)
    payload=dict(forecasts=[dict(symbol='A',source_time=(received-timedelta(seconds=1)).isoformat(),
        received_at=received.isoformat(),next_funding_time=(AT+timedelta(hours=8)).isoformat(),source='authenticated_premium_index')])
    return dict(account_id=account,evidence_sha256=canonical(payload)[1],available_at=at.isoformat(),payload=payload)


def sources_with_ref(tmp_path):
    sources,bootstrap=captured_sources(tmp_path)
    end=AT+timedelta(hours=3)
    rows=[observation(AT-timedelta(seconds=40)+timedelta(microseconds=17)),observation(AT+timedelta(seconds=20,microseconds=59))]
    ref,_=build_forecasts_ref('test',end,rows,expected_count=2)
    parent=sources['equity_evidence'][0]
    parent['payload'].update(account_id='test',capture=dict(funding_publication=dict(forecasts_ref=ref)))
    parent['account_id']='test';parent['evidence_sha256']=canonical(parent['payload'])[1]
    sources['hrs_funding_observations']=rows
    sources['hrs_funding_observations_inventory']=dict(version='hrs-funding-observation-prefix-export-v1',
        account_id='test',available_through=end.isoformat(),expected_count=2)
    return sources,bootstrap


def run_export(sources,bootstrap,tmp_path):
    return export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)


def test_forecast_reference_closure_is_required_before_sealing(tmp_path):
    sources,bootstrap=sources_with_ref(tmp_path)
    del sources['hrs_funding_observations']
    result=run_export(sources,bootstrap,tmp_path)
    assert result.get('decision')=='BLOCKED_DATA',result
    assert any('forecast' in gap for gap in result['gaps'])


@pytest.mark.parametrize('damage',['missing','extra','payload','foreign_account','future','version','parent_hash','parent_future','missing_ref'])
def test_forecast_reference_rejects_unproven_sources_without_rewriting_parent(tmp_path,damage):
    sources,bootstrap=sources_with_ref(tmp_path)
    parent=sources['equity_evidence'][0]
    if damage=='missing':sources['hrs_funding_observations'].pop()
    elif damage=='extra':
        sources['hrs_funding_observations'].append(observation(AT+timedelta(seconds=30)))
        sources['hrs_funding_observations_inventory']['expected_count']=3
    elif damage=='payload':sources['hrs_funding_observations'][0]['payload']['forecasts'][0]['symbol']='BAD'
    elif damage=='foreign_account':sources['hrs_funding_observations'][0]['account_id']='other'
    elif damage=='future':
        sources['hrs_funding_observations'].append(observation(AT+timedelta(hours=3,seconds=1)))
        sources['hrs_funding_observations_inventory']['expected_count']=3
    elif damage=='version':
        parent['payload']['capture']['funding_publication']['forecasts_ref']['version']='future-version'
        parent['evidence_sha256']=canonical(parent['payload'])[1]
    elif damage=='parent_hash':parent['payload']['capture']['funding_publication']['forecasts_ref']['row_count']=0
    elif damage=='parent_future':
        parent['payload']['capture']['funding_publication']['forecasts_ref']['available_through']=(AT+timedelta(hours=3,seconds=1)).isoformat()
        parent['evidence_sha256']=canonical(parent['payload'])[1]
    elif damage=='missing_ref':
        parent['payload']['capture']['funding_publication']={}
        parent['evidence_sha256']=canonical(parent['payload'])[1]
    original=copy.deepcopy(parent)
    result=run_export(sources,bootstrap,tmp_path)
    assert result.get('decision')=='BLOCKED_DATA',result
    assert parent==original
    if damage=='parent_hash':assert any('equity_payload_hash_mismatch' in gap or 'parent_hash' in gap for gap in result['gaps'])


def test_valid_reference_preserves_raw_parent_and_legacy_inline_still_exports(tmp_path):
    sources,bootstrap=sources_with_ref(tmp_path)
    original=copy.deepcopy(sources['equity_evidence'][0])
    result=run_export(sources,bootstrap,tmp_path)
    assert result.get('status')=='SEALED',result
    assert sources['equity_evidence'][0]==original
    retained=json.loads((Path(result['snapshot_dir'])/'raw_hrs_evidence.json').read_text())
    assert retained['equity_evidence'][0]==original
    inline=copy.deepcopy(sources);parent=inline['equity_evidence'][0]
    parent['payload']['capture']['funding_publication']=dict(forecasts=[row['payload']['forecasts'][0] for row in inline.pop('hrs_funding_observations')])
    inline.pop('hrs_funding_observations_inventory');parent['evidence_sha256']=canonical(parent['payload'])[1]
    result=run_export(inline,bootstrap,tmp_path/'legacy')
    assert result.get('status')=='SEALED',result


def test_many_parent_refs_read_source_prefix_once_not_quadratically():
    from ai_tuner.hrs_forecast_sources import verify_parent_closure
    rows=[observation(AT+timedelta(seconds=i)) for i in range(80)]
    parents=[]
    for index,row in enumerate(rows,1):
        cutoff=AT+timedelta(seconds=index-1)
        ref,_=build_forecasts_ref('test',cutoff,rows[:index],expected_count=index)
        payload=dict(account_id='test',available_at=cutoff.isoformat(),capture=dict(funding_publication=dict(forecasts_ref=ref)))
        parents.append(dict(account_id='test',payload=payload,evidence_sha256=canonical(payload)[1]))
    reads=calls=count_calls=0
    def source_rows(cutoff):
        nonlocal reads,calls
        calls+=1
        for row in rows:
            if row['available_at']<=cutoff.isoformat():reads+=1;yield row
    def source_count(cutoff):
        nonlocal count_calls
        count_calls+=1
        return sum(1 for row in rows if cutoff is None or row['available_at']<=cutoff.isoformat())
    inventory=dict(version='hrs-funding-observation-prefix-export-v1',account_id='test',
        available_through=rows[-1]['available_at'],expected_count=len(rows))
    summary=verify_parent_closure(parents,source_rows=source_rows,source_count=source_count,inventory=inventory)
    assert summary['reference_count']==len(parents)
    assert reads==len(rows) and calls==1 and count_calls<=2


def write_archive(sources,path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter
    with EvidenceArchiveWriter(path,source_metadata={}) as writer:
        for family,rows in sources.items():
            if not isinstance(rows,list):continue
            metadata=dict(expected_count=len(rows),query_scope='independent engineering fixture inventory',account_id=None,
                window_start=(AT-timedelta(days=31)).isoformat(),window_end=(AT+timedelta(hours=4)).isoformat())
            if family=='hrs_funding_observations':
                metadata.update(account_id='test',expected_count=sources['hrs_funding_observations_inventory']['expected_count'],
                    prefix_inventory=sources['hrs_funding_observations_inventory'])
            writer.declare_source(family,metadata)
            for row in rows:writer.write_row(family,row)
        return dict(evidence_archive=writer.finish())


def test_archive_closure_rejects_dangling_prefix_and_keeps_parent_hash(tmp_path):
    sources,bootstrap=sources_with_ref(tmp_path)
    original=copy.deepcopy(sources['equity_evidence'][0])
    archived=write_archive(sources,tmp_path/'archive')
    result=run_export(archived,bootstrap,tmp_path)
    assert result.get('status')=='SEALED',result
    from ai_tuner.hrs_evidence_export import _ArchiveSources
    with _ArchiveSources(archived) as view:
        assert view.forecast_closure['source_count']==2
        assert view.archive.get_record('equity_evidence',original['evidence_sha256'])['payload']==original['payload']
    missing=copy.deepcopy(sources);missing.pop('hrs_funding_observations');missing.pop('hrs_funding_observations_inventory')
    archived=write_archive(missing,tmp_path/'missing')
    blocked=run_export(archived,bootstrap,tmp_path/'blocked')
    assert blocked['decision']=='BLOCKED_DATA'
    assert any('forecast' in gap for gap in blocked['gaps'])


def test_source_family_hash_identity_and_prefix_counts_need_no_hrs_import(tmp_path,monkeypatch):
    import builtins
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter,EvidenceArchive,ArchiveError
    row=observation(AT)
    meta=dict(expected_count=1,query_scope='independent fixed source count',account_id='test',
        window_start=(AT-timedelta(hours=1)).isoformat(),window_end=(AT+timedelta(hours=1)).isoformat())
    original=builtins.__import__
    def guard(name,*args,**kwargs):
        if name.startswith('strategies.hrs'):raise ModuleNotFoundError('no HRS runtime modules')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guard)
    with EvidenceArchiveWriter(tmp_path/'pure_archive',source_metadata={'hrs_funding_observations':meta}) as writer:
        writer.write_row('hrs_funding_observations',row);descriptor=writer.finish()
    with EvidenceArchive.open(descriptor['path']) as archive:
        assert archive.get_record('hrs_funding_observations',row['evidence_sha256'])==row
        assert archive.count_records('hrs_funding_observations',available_before=AT)==1
        assert archive.count_records('hrs_funding_observations',available_before=AT-timedelta(microseconds=1))==0
    corrupt=copy.deepcopy(row);corrupt['payload']['forecasts'][0]['symbol']='BAD'
    with EvidenceArchiveWriter(tmp_path/'corrupt',source_metadata={'hrs_funding_observations':meta}) as writer:
        with pytest.raises(ArchiveError,match='hash'):writer.write_row('hrs_funding_observations',corrupt)


@pytest.mark.skipif(not os.environ.get('HRS_TEST_POSTGRES_DSN'),reason='isolated PostgreSQL administrator DSN required')
async def test_real_readonly_postgres_reference_source_to_archive_and_cli(tmp_path):
    """All SQL is real on an isolated local DB; market facts are engineering fixtures."""
    import asyncpg
    from ai_tuner.hrs_evidence_export import read_evidence_sources,_ArchiveSources
    from ai_tuner import hrs_exit_experiment as experiment
    from tests.test_ai_tuner_mvp.test_hrs_exit_experiment import registration
    from strategies.hrs.decision_capture import canonical_payload
    import hashlib
    control=await asyncpg.connect(os.environ['HRS_TEST_POSTGRES_DSN'])
    database='hrs_forecast_export_'+uuid.uuid4().hex
    role='hrs_forecast_reader_'+uuid.uuid4().hex
    admin=reader=None
    try:
        await control.execute('CREATE DATABASE '+database+" TEMPLATE template0 ENCODING 'UTF8'")
        await control.execute('CREATE ROLE '+role+' NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
        admin=await asyncpg.connect(os.environ['HRS_TEST_POSTGRES_DSN'],database=database)
        await admin.execute('''CREATE SCHEMA hrs;CREATE SCHEMA trading;
            CREATE TABLE hrs.hrs_decision_evidence(evidence_id text PRIMARY KEY,run_id text,kind text,symbol text,event_time timestamptz,available_at timestamptz,payload_sha256 text,payload jsonb);
            CREATE TABLE trading.hrs_equity_evidence(account_id text,evidence_sha256 text,segment_id text,as_of timestamptz,available_at timestamptz,payload jsonb,PRIMARY KEY(account_id,evidence_sha256));
            CREATE TABLE trading.hrs_risk_equity_checkpoints(account_id text,segment_id text,as_of timestamptz,equity numeric,units numeric,nav numeric,created_at timestamptz);
            CREATE TABLE public.ai_tuner_funding_schedule_observations(symbol text,source_time timestamptz,received_at timestamptz,next_funding_time timestamptz,funding_interval_hours int,payload_hash text);
            CREATE TABLE trading.hrs_funding_observation(account_id text,evidence_sha256 text,available_at timestamptz,payload jsonb,PRIMARY KEY(account_id,evidence_sha256));''')
        sources,bootstrap=sources_with_ref(tmp_path)
        instant=lambda value:datetime.fromisoformat(value.replace('Z','+00:00'))
        for row in sources['decision_evidence']:
            if row['kind']=='scan_start':row['payload']['financial_context']['entry_policy']['account_id']='test'
            elif row['kind']=='scan_end':row['payload']['state']['financial_context']['entry_policy']['account_id']='test'
            row['payload_sha256']=canonical_payload(row['payload'])[1]
            await admin.execute('INSERT INTO hrs.hrs_decision_evidence VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)',
                row['evidence_id'],row['run_id'],row['kind'],row['symbol'],instant(row['event_time']),instant(row['available_at']),row['payload_sha256'],json.dumps(row['payload']))
        parent=sources['equity_evidence'][0]
        await admin.execute('INSERT INTO trading.hrs_equity_evidence VALUES($1,$2,$3,$4,$5,$6::jsonb)',
            'test',parent['evidence_sha256'],'test',instant(parent['payload']['as_of']),instant(parent['payload']['available_at']),json.dumps(parent['payload']))
        for row in sources['equity_checkpoints']:
            await admin.execute('INSERT INTO trading.hrs_risk_equity_checkpoints VALUES($1,$2,$3,$4,$5,$6,$7)',
                'test','test',instant(row['as_of']),Decimal(row['equity']),Decimal(row['units']),Decimal(row['nav']),instant(row['as_of']))
        for row in sources['funding_observations']:
            await admin.execute('INSERT INTO public.ai_tuner_funding_schedule_observations VALUES($1,$2,$3,$4,$5,$6)',
                row['symbol'],instant(row['source_time']),instant(row['received_at']),instant(row['next_funding_time']),row['funding_interval_hours'],row['payload_hash'])
        extras=[observation(AT+timedelta(seconds=5),account='other'),observation(AT+timedelta(hours=3,seconds=1))]
        for row in [*sources['hrs_funding_observations'],*extras]:
            await admin.execute('INSERT INTO trading.hrs_funding_observation VALUES($1,$2,$3,$4::jsonb)',
                row['account_id'],row['evidence_sha256'],instant(row['available_at']),json.dumps(row['payload']))
        await admin.execute('GRANT USAGE ON SCHEMA hrs,trading,public TO '+role)
        await admin.execute('GRANT SELECT ON ALL TABLES IN SCHEMA hrs,trading,public TO '+role)
        reader=await asyncpg.connect(os.environ['HRS_TEST_POSTGRES_DSN'],database=database)
        await reader.execute('SET ROLE '+role)
        assert await reader.fetchval('SELECT NOT rolsuper FROM pg_roles WHERE rolname=current_user')
        with pytest.raises(asyncpg.InsufficientPrivilegeError):await reader.execute("DELETE FROM trading.hrs_funding_observation WHERE false")
        await reader.execute('SET default_transaction_read_only=on')
        assert await reader.fetchval('SHOW default_transaction_read_only')=='on'
        market=tmp_path/'market';market.mkdir();queries={}
        for batch in sources['market_bars']:
            name='bars_'+batch['interval']+'.jsonl';body=''.join(json.dumps(row)+'\n' for row in batch['rows'])
            (market/name).write_text(body)
            queries['btc_eth.kline_a_'+batch['interval']]=dict(file=name,row_count=len(batch['rows']),sha256=hashlib.sha256(body.encode()).hexdigest(),
                min_time=min(row['open_time'] for row in batch['rows']),max_time=max(row['open_time'] for row in batch['rows']))
        (market/'raw_manifest.json').write_text(json.dumps(dict(strategy_id='hrs',queries=queries)))
        archived=await read_evidence_sources(reader,'test',window_start=AT,as_of=AT+timedelta(hours=3),archive_root=tmp_path/'archive',raw_market_export=market)
        with _ArchiveSources(archived) as view:
            assert view.forecast_closure['source_count']==2
            metadata=view.archive.source_metadata['hrs_funding_observations']
            assert metadata['prefix_inventory']['expected_count']==2
            assert 'available_at<=' in metadata['query_scope']['sql'] and 'available_at>=' not in metadata['query_scope']['sql']
            rebuilt=list(view.archive.iter_records('hrs_funding_observations',order_by_available=True))
            assert rebuilt==sources['hrs_funding_observations']
            assert view.archive.get_record('hrs_funding_observations',extras[0]['evidence_sha256']) is None
            assert view.archive.get_record('hrs_funding_observations',extras[1]['evidence_sha256']) is None
        result=run_export(archived,bootstrap,tmp_path)
        assert result.get('status')=='SEALED',result
        reg=registration();reg['baseline'].update(config=bootstrap['config'],effective_config_sha256=experiment.fingerprint(bootstrap['config']))
        reg['windows'][0].update(start=AT.isoformat(),end=(AT+timedelta(hours=3)).isoformat(),snapshot_dir=result['snapshot_dir'])
        reg['windows'][1].update(start='2026-12-01T00:00:00+08:00',end='2027-01-01T00:00:00+08:00')
        reg['windows'][2].update(start='2027-01-01T00:00:00+08:00',end='2027-02-01T00:00:00+08:00')
        report=experiment.run_experiment(reg,snapshot_roots=[result['snapshot_dir']])
        assert report['engine_runs']==6,report['blocked_reasons']
        assert report['performance_accepted'] is False
    finally:
        if reader is not None:await reader.close()
        if admin is not None:await admin.close()
        await control.execute('DROP DATABASE IF EXISTS '+database+' WITH (FORCE)')
        await control.execute('DROP ROLE IF EXISTS '+role)
        await control.close()
