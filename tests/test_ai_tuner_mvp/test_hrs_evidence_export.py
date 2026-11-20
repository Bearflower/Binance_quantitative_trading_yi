"""Raw-capture conversion contracts; engineering fixtures are never live evidence."""
import copy
import json
import pytest
from datetime import timedelta
from pathlib import Path

from strategies.hrs.decision_capture import canonical_payload
from tests.test_ai_tuner_mvp.replay.test_hrs_wiring import _hrs_fixture,AT,broker


def envelope(run,kind,payload,at,symbol=None):
    encoded,digest=canonical_payload(payload)
    return dict(evidence_id=run+'-'+kind+'-'+str(symbol)+'-'+at.isoformat()+'-'+digest,run_id=run,kind=kind,
        symbol=symbol,event_time=at.isoformat(),available_at=at.isoformat(),payload_sha256=digest,payload=json.loads(encoded))


def captured_sources(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    bootstrap=json.loads((portal.snapshot_dir/'hrs_replay_state.json').read_text())
    states=adapter._state_loader(AT)
    runtime=bootstrap['runtime']
    ledger=dict(segment_id='test',inception=bootstrap['ledger']['history_complete_from'],as_of=AT.isoformat(),
        cash_allocation='10000',income_total='0',units='10000',equity='10000',nav='1',incomes=[],cash_flows=[])
    financial=dict(available_at=AT.isoformat(),database_snapshot_coherent=True,replay_bootstrap_ready=True,
        account_equity='10000',hrs_marked_equity='10000',allocation='10000',entry_policy=bootstrap['entry_policy'],
        ledger_state=dict(ledger_state=ledger,gate_state=None),pending_timeout_entries=[],exchange_positions=[],independent_positions=[],reservations=[],claims=[],
        state_tables={name:dict(rows=value['rows'],captured_at=AT.isoformat(),database_snapshot_coherent=True) for name,value in states.items()})
    snapshot=dict(config=bootstrap['config'],config_hashes=bootstrap['config_hashes'],
        financial_context=financial,positions={},market_circuit_breaker_config=None,
        market_circuit_breaker_state=dict(cooldown_until_local={},process_utc_offset_seconds=0),
        runtime=dict(signal_timestamps={},daily_reset_date=runtime['daily_reset_date'],total_pnl='0',
            risk_manager={k:v for k,v in runtime.items() if k not in ('signal_timestamps','daily_reset_date','total_pnl','pending_timeout_entries','market_cooldowns')}))
    records=[envelope('bootstrap','scan_start',snapshot,AT),envelope('bootstrap','scan_denial',dict(reason='settlement_window'),AT)]
    records.append(envelope('bootstrap','scan_end',dict(status='complete',state=snapshot),AT))
    inputs=[json.loads(s) for s in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    for index,row in enumerate(inputs):
        at=AT+timedelta(hours=index+1);run='cycle-'+str(index)
        snap=copy.deepcopy(snapshot);snap['candidate_scan']=row['candidate_scan']
        snap['financial_context']['available_at']=at.isoformat()
        records += [envelope(run,'scan_start',snap,at),envelope(run,'standard_scan_scope',dict(symbols=['A']),at),
            envelope(run,'analysis',dict(result={},klines_1h=[dict(open_time=int(b.open_time.timestamp()*1000),open=str(b.open),high=str(b.high),low=str(b.low),close=str(b.close),volume=str(b.volume),quote_volume=str(b.quote_volume)) for b in portal.bars('A','1h') if b.close_time<=at],
                klines_4h=[dict(open_time=int(b.open_time.timestamp()*1000),open=str(b.open),high=str(b.high),low=str(b.low),close=str(b.close),volume=str(b.volume),quote_volume=str(b.quote_volume)) for b in portal.bars('A','4h') if b.close_time<=at]),at,'A')]
        for operation,key in [('get_oi_usd','oi_usd'),('get_funding_rate','funding_rate'),('get_24h_volume','volume_24h')]:
            records.append(envelope(run,'market_input',dict(operation=operation,arguments=['A'],options={},result=row['market'][key]),at,'A'))
        cap=dict(value=row['market']['market_cap'],source='coingecko',as_of=at.isoformat(),available_at=at.isoformat(),
            expires_at=(at+timedelta(hours=1)).isoformat(),is_proxy=False,missing_reason=None,provider='coingecko',provider_asset_id='engineering-a')
        records += [envelope(run,'market_input',dict(operation='get_market_cap_provenance',arguments=['A'],options={},result=cap),at,'A'),
            envelope(run,'market_cap_batch',dict(requested_symbols=['A'],quotes={'A':cap}),at),
            envelope(run,'entry_stage_inputs',dict(exchange_positions=[],claims=[],pool_index=None,pool_index_12h=None,
                pre_entry_oi_usd=row['market']['oi_usd'],pre_entry_funding_rate=row['market']['funding_rate']),at,'A'),
            envelope(run,'execution_batch',dict(ordered_signals=[dict(symbol='A',direction='long')]),at),
            envelope(run,'scan_end',dict(status='complete',state=snap),at)]
    exchange=json.loads((portal.snapshot_dir/'exchange_info.json').read_text())
    records.append(envelope('bootstrap','public_market_response',dict(path='/fapi/v1/exchangeInfo',params={},response=exchange),AT))
    market=[]
    for interval in ('1h','4h','1m'):
        rows=[json.loads(s) for s in (portal.snapshot_dir/f'klines/A_{interval}.jsonl').read_text().splitlines()]
        market.append(dict(symbol='A',interval=interval,source='postgres_klines',rows=rows,payload_sha256=canonical_payload(rows)[1]))
    equity_payload=dict(as_of=portal.as_of.isoformat(),available_at=portal.as_of.isoformat(),segment_id='test',
        income_report=dict(id_match_and_coverage_complete=True,coverage_gaps=[],missing_income_fills=[],unattributed=[],events=[]),
        source_collection=dict(complete=True,gaps=[]),allocation_audit=dict(version=1,journal=[]),result=dict(ready=True))
    return dict(decision_evidence=records,market_bars=market,
        equity_evidence=[dict(evidence_sha256=canonical_payload(equity_payload)[1],payload=equity_payload)],
        equity_checkpoints=bootstrap['ledger']['history'],
        funding_observations=json.loads((portal.snapshot_dir/'funding_schedule_evidence.json').read_text())['observations']),bootstrap


def test_missing_history_emits_gap_bundle_without_sealed_snapshot(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    result=export_evidence_bundle({},tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3))
    assert result['decision']=='BLOCKED_DATA'
    assert 'decision_evidence_missing' in result['gaps']
    assert Path(result['gap_bundle']).is_file()
    assert not list((tmp_path/'output').rglob('manifest.json'))


def test_raw_captures_roundtrip_into_actual_strategy_engine(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from ai_tuner.replay.data_portal import DataPortal
    from ai_tuner.replay.engine import run_replay
    sources,bootstrap=captured_sources(tmp_path)
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    portal=DataPortal(result['snapshot_dir'],'hrs',AT+timedelta(hours=3),AT)
    states=json.loads((Path(result['snapshot_dir'])/'hrs_state_tables.json').read_text())
    adapter=HrsAdapter(lambda at:states)
    replay=run_replay(portal,adapter,broker())
    assert replay.final_position and replay.final_position.entry_mode=='standard'
    assert any(e['type']=='hrs_budget' for e in replay.audit_events)
    assert result['source_attested'] is False


def test_registered_lower_bound_resolves_first_actual_scan_without_backdating(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    lower=AT-timedelta(minutes=21)
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=lower,as_of=AT+timedelta(hours=3),
        start_policy=dict(version='first_scan_start_v1',max_delay_seconds=3600),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    path=Path(result['snapshot_dir'])
    receipt=json.loads((path/'hrs_export_receipt.json').read_text())
    assert receipt['boundary_resolution']['actual_start']=='2026-11-17T00:00:00Z'
    assert receipt['boundary_resolution']['delay_seconds']==1260
    assert json.loads((path/'manifest.json').read_text())['window_start']=='2026-11-17T00:00:00Z'


def test_boundary_selection_cannot_skip_a_nonflat_first_scan(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    first=sources['decision_evidence'][0]
    first['payload']['positions']={'A':{'remaining_quantity':'1'}}
    first['payload_sha256']=canonical_payload(first['payload'])[1]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT-timedelta(minutes=1),as_of=AT+timedelta(hours=3),
        start_policy=dict(version='first_scan_start_v1',max_delay_seconds=3600))
    assert result['decision']=='BLOCKED_DATA'
    assert 'nonflat_opening_cost_mae_funding_proof_missing' in result['gaps']


def test_boundary_selection_cannot_skip_an_incomplete_first_scan(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,_=captured_sources(tmp_path)
    sources['decision_evidence']=[r for r in sources['decision_evidence'] if not (r['run_id']=='bootstrap' and r['kind']=='scan_end')]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT-timedelta(minutes=1),as_of=AT+timedelta(hours=3),
        start_policy=dict(version='first_scan_start_v1',max_delay_seconds=3600))
    assert result['decision']=='BLOCKED_DATA'
    assert 'first_scan_boundary_incomplete' in result['gaps']


def test_boundary_resolver_rejects_late_or_ambiguous_first_scan():
    from ai_tuner.hrs_evidence_export import resolve_scan_start
    from ai_tuner.replay.models import DataBlocked
    policy=dict(version='first_scan_start_v1',max_delay_seconds=3600)
    with pytest.raises(DataBlocked,match='first_scan_boundary_missing'):
        resolve_scan_start([envelope('late','scan_start',{},AT+timedelta(hours=1,seconds=1))],AT,AT+timedelta(days=1),policy)
    with pytest.raises(DataBlocked,match='first_scan_boundary_ambiguous'):
        resolve_scan_start([envelope('a','scan_start',{},AT),envelope('b','scan_start',{},AT)],AT,AT+timedelta(days=1),policy)


@pytest.mark.parametrize('delayed_availability',[False,True])
def test_both_preregistered_boundaries_resolve_real_scans_and_exclude_terminal_cycle(tmp_path,delayed_availability):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    if delayed_availability:
        for row in sources['decision_evidence']:
            at=AT if row['run_id']=='bootstrap' else AT+timedelta(hours=1)
            if row['run_id']=='bootstrap' and row['kind']=='scan_start':
                row['available_at']=(at+timedelta(seconds=5,microseconds=123456)).isoformat()
                row['payload']['financial_context']['available_at']=(at+timedelta(seconds=4,microseconds=123456)).isoformat()
            elif row['run_id']=='cycle-0':
                seconds={'scan_start':0,'analysis':6,'market_input':2,'entry_stage_inputs':7,
                         'execution_batch':8,'scan_end':10}.get(row['kind'],1)
                row['event_time']=(at+timedelta(seconds=seconds,microseconds=123456)).isoformat()
                row['available_at']=(at+timedelta(seconds=seconds+1,microseconds=123456)).isoformat()
                if row['kind']=='scan_start':
                    row['available_at']=(at+timedelta(seconds=5,microseconds=123456)).isoformat()
                    row['payload']['financial_context']['available_at']=(at+timedelta(seconds=4,microseconds=123456)).isoformat()
                if row['kind']=='scan_end':
                    row['payload']['state']['financial_context']['available_at']=(at+timedelta(seconds=4,microseconds=123456)).isoformat()
            row['payload_sha256']=canonical_payload(row['payload'])[1]
    end=AT+timedelta(hours=3)
    # The end scan is also the next window's first scan; its decision belongs
    # to that next window, and no synthetic decision is added to this one.
    policy=dict(version='first_scan_start_v1',max_delay_seconds=3600)
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT-timedelta(minutes=21),as_of=end-timedelta(minutes=15),
        start_policy=policy,end_policy=policy,execution_model=bootstrap['execution_model'],
        monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    path=Path(result['snapshot_dir']);receipt=json.loads((path/'hrs_export_receipt.json').read_text())
    if delayed_availability:
        assert receipt['bootstrap_available_at']=='2026-11-17T00:00:05.123456Z'
        calendar=[json.loads(line) for line in (path/'hrs_decision_calendar.jsonl').read_text().splitlines()]
        assert calendar[0]['decision_at']=='2026-11-17T01:00:06.123456Z'
        assert calendar[0]['available_at']=='2026-11-17T01:00:09.123456Z'
    assert receipt['end_boundary_resolution']['actual_start']=='2026-11-17T03:00:00Z'
    assert json.loads((path/'manifest.json').read_text())['as_of']=='2026-11-17T03:00:00Z'
    from .test_hrs_exit_experiment import registration
    from ai_tuner import hrs_exit_experiment as experiment
    registered=registration();registered['baseline'].update(config=bootstrap['config'],
        effective_config_sha256=experiment.fingerprint(bootstrap['config']))
    registered['windows'][0].update(start=(AT-timedelta(minutes=21)).isoformat(),end=(end-timedelta(minutes=15)).isoformat(),
        start_policy=policy,end_policy=policy,snapshot_dir=str(path))
    registered['windows'][1].update(start='2026-12-01T00:00:00+08:00',end='2027-01-01T00:00:00+08:00')
    registered['windows'][2].update(start='2027-01-01T00:00:00+08:00',end='2027-02-01T00:00:00+08:00')
    report=experiment.run_experiment(registered,snapshot_roots=[path])
    assert report['engine_runs']==6,report['blocked_reasons']
    assert report['window_boundaries']['research']['end_delay_seconds']==900
    assert report['window_boundaries']['research']['actual_duration_seconds']==3*3600
    # Integrity-preserving re-sealing of a false resolution must still fail the
    # independent raw inventory re-computation before any strategy execution.
    from ai_tuner.replay.data_portal import compute_manifest_envelope_sha256,compute_manifest_sha256,sha256_file
    receipt['boundary_resolution']['selected_evidence_id']='later-chosen-scan'
    (path/'hrs_export_receipt.json').write_text(json.dumps(receipt))
    manifest=json.loads((path/'manifest.json').read_text())
    manifest['files']['hrs_export_receipt.json']['sha256']=sha256_file(str(path/'hrs_export_receipt.json'))
    manifest['sha256']=compute_manifest_sha256(manifest['files'])
    manifest['envelope_sha256']=compute_manifest_envelope_sha256(manifest)
    (path/'manifest.json').write_text(json.dumps(manifest))
    blocked=experiment.run_experiment(registered,snapshot_roots=[path])
    assert blocked['engine_runs']==0
    assert any('scan_boundary_resolution_mismatch' in gap for gap in blocked['blocked_reasons'])


def test_tampered_payload_and_incomplete_scan_are_not_exportable(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,_=captured_sources(tmp_path)
    sources['decision_evidence'][0]['payload']['config']['trading']['leverage']=99
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3))
    assert result['decision']=='BLOCKED_DATA'
    assert any('payload_hash' in gap for gap in result['gaps'])


def test_missing_execution_minutes_never_leaves_a_manifest(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    source=next(row for row in sources['market_bars'] if row['interval']=='1m')
    source['rows'].pop(70);source['payload_sha256']=canonical_payload(source['rows'])[1]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'])
    assert result['decision']=='BLOCKED_DATA'
    assert any('subbar_coverage' in g for g in result['gaps'])
    assert not list((tmp_path/'output').rglob('manifest.json'))


def test_complete_scan_inventory_cannot_omit_a_vetoed_symbol(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    row=next(r for r in sources['decision_evidence'] if r['kind']=='standard_scan_scope')
    row['payload']['symbols'].append('B');row['payload_sha256']=canonical_payload(row['payload'])[1]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'])
    assert result['decision']=='BLOCKED_DATA'
    assert any('raw_analysis_missing' in g for g in result['gaps'])


def test_actual_off_hour_availability_survives_export(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    for row in sources['decision_evidence']:
        if row['run_id']=='cycle-0':
            row['event_time']=(AT+timedelta(hours=1,minutes=21)).isoformat()
            row['available_at']=(AT+timedelta(hours=1,minutes=21,seconds=5)).isoformat()
            if row['kind']=='scan_start':
                row['payload']['financial_context']['available_at']=row['available_at']
                row['payload_sha256']=canonical_payload(row['payload'])[1]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    calendar=[json.loads(s) for s in (Path(result['snapshot_dir'])/'hrs_decision_calendar.jsonl').read_text().splitlines()]
    assert calendar[0]['available_at']=='2026-11-17T01:21:05Z'
    assert calendar[0]['decision_at']=='2026-11-17T01:21:00Z'


def test_retained_scores_are_audit_only_not_replay_actions(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    for row in sources['decision_evidence']:
        if row['kind']=='analysis':
            row['payload']['result']={'should_entry':False,'chosen_action':'SELL_EVERYTHING'}
            row['payload_sha256']=canonical_payload(row['payload'])[1]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    raw=[json.loads(s) for s in (Path(result['snapshot_dir'])/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    assert not any('chosen_action' in row or 'should_entry' in row for row in raw)


def test_proven_flat_bridge_retains_real_later_availability(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    previous=AT-timedelta(seconds=30);received=AT+timedelta(seconds=10)
    row=next(r for r in sources['decision_evidence'] if r['kind']=='scan_start' and r['run_id']=='bootstrap')
    financial=row['payload']['financial_context'];financial['available_at']=received.isoformat();financial['reservations']=[]
    financial['ledger_state']['ledger_state']['as_of']=previous.isoformat()
    row['payload_sha256']=canonical_payload(row['payload'])[1];row['available_at']=received.isoformat()
    for at in (previous,received):
        p=dict(account_id='test',as_of=at.isoformat(),available_at=at.isoformat(),segment_id='test',
            capture=dict(local={},inventory=[],independent=[]),fills=[],order_owners=[],
            income_report=dict(id_match_and_coverage_complete=True,coverage_gaps=[],missing_income_fills=[],unattributed=[],events=[]),
            source_collection=dict(complete=True,gaps=[]),allocation_audit=dict(version=1,journal=[]),result=dict(ready=True))
        sources['equity_evidence'].append(dict(evidence_sha256=canonical_payload(p)[1],payload=p))
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    state=json.loads((Path(result['snapshot_dir'])/'hrs_replay_state.json').read_text())
    assert state['bootstrap_bridge']['version']=='proven_flat_unchanged_v1'
    assert state['bootstrap_available_at']=='2026-11-17T00:00:10Z'


def test_raw_market_export_hashes_and_utc_bindings_are_checked(tmp_path):
    from ai_tuner.hrs_evidence_export import read_market_export
    import hashlib
    root=tmp_path/'raw';root.mkdir()
    payload=json.dumps(dict(open_time=AT.isoformat(),close_time=(AT+timedelta(minutes=1)).isoformat(),open_price='100',high_price='101',low_price='99',close_price='100',volume='5',quote_volume='500',trade_count=1))+'\n'
    (root/'bars.jsonl').write_text(payload)
    manifest=dict(strategy_id='hrs',queries={'btc_eth.kline_a_1m':dict(file='bars.jsonl',sha256=hashlib.sha256(payload.encode()).hexdigest(),row_count=1)})
    (root/'raw_manifest.json').write_text(json.dumps(manifest))
    assert read_market_export(root)[0]['rows'][0]['open_price']=='100'
    (root/'bars.jsonl').write_text(payload.replace('100','200'))
    import pytest
    with pytest.raises(Exception,match='raw_market_export_hash'):
        read_market_export(root)


async def test_readonly_database_extraction_bounds_rows_and_preserves_availability(tmp_path):
    from ai_tuner.hrs_evidence_export import read_evidence_sources
    class Transaction:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class DB:
        async def fetchval(self,*args):return None
        queries=[]
        def transaction(self,**kwargs):
            assert kwargs=={'readonly':True,'isolation':'repeatable_read'}
            return Transaction()
        async def fetch(self,query,*args):
            self.queries.append((query,args))
            return []
    db=DB();result=await read_evidence_sources(db,'test',window_start=AT,as_of=AT+timedelta(hours=3))
    assert not result['decision_evidence']
    assert len(db.queries)>=4
    assert all(query.lstrip().startswith('SELECT') and 'LIMIT' in query for query,_ in db.queries)


async def test_archive_database_extraction_streams_beyond_legacy_whole_window_caps(tmp_path):
    from ai_tuner.hrs_evidence_export import read_evidence_sources
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchive
    class Transaction:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class DB:
        async def fetchval(self,*args):return None
        def transaction(self,**options):
            assert options==dict(readonly=True,isolation='repeatable_read')
            return Transaction()
        async def fetch(self,*args):raise AssertionError('whole source fetch is forbidden')
        async def fetchrow(self,query,*args):
            assert 'count(*)' in query
            return dict(expected_count=50001,first_event=AT,last_event=AT+timedelta(seconds=50000)) if 'FROM hrs.hrs_decision_evidence' in query else dict(expected_count=0,first_event=None,last_event=None)
        def cursor(self,query,*args,**options):
            async def rows():
                if 'FROM hrs.hrs_decision_evidence' not in query:return
                payload=dict(path='/fapi/v1/exchangeInfo',params={},response=dict(repeated='x'*1536))
                digest=canonical_payload(payload)[1]
                for index in range(50001):
                    at=AT+timedelta(seconds=index)
                    yield dict(evidence_id=str(index),run_id='background',kind='public_market_response',symbol=None,
                        event_time=at,available_at=at,payload_sha256=digest,payload=payload)
            return rows()
    source=await read_evidence_sources(DB(),'test',window_start=AT,as_of=AT+timedelta(days=31),archive_root=tmp_path/'archive')
    with EvidenceArchive.open(source['evidence_archive']['path']) as archive:
        assert archive.counts['decision_evidence']==50001
        rows=archive.iter_records('decision_evidence')
        assert next(rows)['available_at']==AT.isoformat()
        assert sum(1 for _ in rows)==50000


def test_large_lossless_archive_roundtrips_to_real_engine_and_causal_cli(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter,EvidenceArchive
    from ai_tuner.hrs_evidence_export import export_evidence_bundle,MAX_BYTES
    from ai_tuner.replay.data_portal import DataPortal
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from ai_tuner.replay.engine import run_replay
    from .test_hrs_exit_experiment import registration
    from ai_tuner import hrs_exit_experiment as experiment
    sources,bootstrap=captured_sources(tmp_path)
    payload=dict(path='/fapi/v1/fundingInfo',params={},response=[dict(symbol='A',fundingIntervalHours=8,
        engineering_padding='x'*1536)])
    digest=canonical_payload(payload)[1]
    archive_root=tmp_path/'archive'
    with EvidenceArchiveWriter(archive_root,source_metadata={}) as writer:
        for family,rows in sources.items():
            first=AT-timedelta(days=31)
            writer.declare_source(family,dict(expected_count=len(rows)+(50001 if family=='decision_evidence' else 0),
                query_scope='independent engineering generator cardinality v1',account_id='test',
                window_start=first.isoformat(),window_end=(AT+timedelta(hours=3)).isoformat()))
            for row in rows:writer.write_row(family,row)
            if family=='decision_evidence':
                for index in range(50001):
                    at=AT+timedelta(microseconds=index+1)
                    writer.write_row(family,dict(evidence_id='receipt-'+str(index),run_id='background',kind='public_market_response',
                        symbol=None,event_time=at.isoformat(),available_at=at.isoformat(),payload_sha256=digest,payload=payload))
        descriptor=writer.finish()
    with EvidenceArchive.open(descriptor['path']) as archive:
        assert archive.counts['decision_evidence']>50000
    assert 50001*len(canonical_payload(payload)[0].encode())>MAX_BYTES
    policy=dict(version='first_scan_start_v1',max_delay_seconds=3600)
    result=export_evidence_bundle(dict(evidence_archive=descriptor),tmp_path/'output',
        window_start=AT-timedelta(minutes=21),as_of=AT+timedelta(hours=2,minutes=45),start_policy=policy,end_policy=policy,
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    path=Path(result['snapshot_dir']);states=json.loads((path/'hrs_state_tables.json').read_text())
    replay=run_replay(DataPortal(path,'hrs',AT+timedelta(hours=3),AT),HrsAdapter(lambda at:states),broker())
    assert any(row['type']=='hrs_score' for row in replay.audit_events)
    assert any(row['type']=='hrs_budget' for row in replay.audit_events)
    assert all('analysis_klines_1h' not in row['payload']['inputs'] for row in replay.audit_events if row['type']=='hrs_score')
    r=registration();r['baseline'].update(config=bootstrap['config'],effective_config_sha256=experiment.fingerprint(bootstrap['config']))
    r['windows'][0].update(start=(AT-timedelta(minutes=21)).isoformat(),end=(AT+timedelta(hours=2,minutes=45)).isoformat(),
        start_policy=policy,end_policy=policy,snapshot_dir=str(path))
    r['windows'][1].update(start='2026-12-01T00:00:00+08:00',end='2027-01-01T00:00:00+08:00')
    r['windows'][2].update(start='2027-01-01T00:00:00+08:00',end='2027-02-01T00:00:00+08:00')
    report=experiment.run_experiment(r,snapshot_roots=[path])
    assert report['engine_runs']==6,report['blocked_reasons']
    assert report['window_boundaries']['research']['actual_duration_seconds']==10800


async def test_market_cursor_supports_52_symbols_and_all_three_intervals_without_legacy_pair_cap(tmp_path):
    from ai_tuner.hrs_evidence_export import _stream_market_tables,MAX_EXECUTION_BARS,MAX_DERIVED_EVENTS
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter,EvidenceArchive
    import re
    names=('id','open_time','close_time','open_price','high_price','low_price','close_price','volume','quote_volume','trade_count')
    def size(query):return 1001 if '_1m' in query else 25 if '_1h' in query else 7
    class DB:
        async def fetchval(self,*args):return None
        async def fetch(self,query,*args):
            assert 'information_schema.columns' in query
            return [dict(column_name=name,data_type='timestamp with time zone' if name.endswith('_time') else 'numeric') for name in names]
        async def fetchrow(self,query,*args):
            assert 'count(*)' in query
            return dict(expected_count=size(query),first_event=AT,last_event=AT+timedelta(days=2))
        def cursor(self,query,*args,**kwargs):
            async def rows():
                interval=re.search(r'_(1m|1h|4h) AS',query).group(1)
                minutes={'1m':1,'1h':60,'4h':240}[interval]
                for i in range(size(query)):
                    yield dict(id=i,open_time=AT+timedelta(minutes=i*minutes),close_time=AT+timedelta(minutes=(i+1)*minutes),
                        open_price='100',high_price='101',low_price='99',close_price='100',volume='1',quote_volume='100',trade_count=1)
            return rows()
    pairs=[('T'+str(i),interval) for i in range(52) for interval in ('1m','1h','4h')]
    with EvidenceArchiveWriter(tmp_path/'market',source_metadata={}) as writer:
        await _stream_market_tables(DB(),writer,pairs,AT,AT+timedelta(days=31),{})
        descriptor=writer.finish()
    with EvidenceArchive.open(descriptor['path']) as archive:
        assert archive.source_metadata['market_bars']['expected_item_count']==52*(1001+25+7)
        assert archive.counts['market_bars']==52*4
    assert MAX_EXECUTION_BARS>=31*52*1440
    assert MAX_DERIVED_EVENTS>31*52*(1440+24+6)


def test_streamed_funding_schedule_matches_legacy_and_retains_gap_denial(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter
    from ai_tuner.hrs_evidence_export import _ArchiveSources
    from ai_tuner.replay.funding_history import attested_funding_schedule
    from ai_tuner.replay.models import DataBlocked
    sources,_=captured_sources(tmp_path)
    end=AT+timedelta(hours=3)
    for missing in (False,True):
        rows=[row for row in sources['funding_observations'] if not missing or row['received_at']!=(AT+timedelta(minutes=5)).isoformat()]
        with EvidenceArchiveWriter(tmp_path/('funding-'+str(missing)),source_metadata={}) as writer:
            writer.declare_source('funding_observations',dict(expected_count=len(rows),query_scope='engineering independent inventory',
                account_id=None,window_start=(AT-timedelta(minutes=10)).isoformat(),window_end=end.isoformat()))
            for row in reversed(rows):writer.write_row('funding_observations',row)
            descriptor=writer.finish()
        with _ArchiveSources(dict(evidence_archive=descriptor)) as stream:
            legacy=dict(source='prospective_exchange_forecast',complete=True,observations=rows)
            archived=dict(legacy,observations=stream.get('funding_observations'))
            if missing:
                with pytest.raises(DataBlocked,match='coverage_gap'):attested_funding_schedule(archived,('A',),AT,end)
            else:
                assert attested_funding_schedule(archived,('A',),AT,end)==attested_funding_schedule(legacy,('A',),AT,end)


@pytest.mark.parametrize('embedded_market',[False,True])
def test_export_cli_accepts_archive_inventory_and_streamed_legacy_market_merge(tmp_path,embedded_market):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter
    from ai_tuner.hrs_evidence_export import main
    import hashlib
    sources,bootstrap=captured_sources(tmp_path)
    with EvidenceArchiveWriter(tmp_path/'archive',source_metadata={}) as writer:
        for family,rows in sources.items():
            if family=='market_bars' and not embedded_market:continue
            writer.declare_source(family,dict(expected_count=len(rows),query_scope='engineering independently sized fixture',account_id=None,
                window_start=(AT-timedelta(days=31)).isoformat(),window_end=(AT+timedelta(hours=3)).isoformat()))
            for row in rows:writer.write_row(family,row)
        descriptor=writer.finish()
    options=[]
    if not embedded_market:
        market=tmp_path/'legacy_market';market.mkdir();queries={}
        for batch in sources['market_bars']:
            name='bars_'+batch['interval']+'.jsonl';body=''.join(json.dumps(row)+'\n' for row in batch['rows'])
            (market/name).write_text(body)
            queries['btc_eth.kline_a_'+batch['interval']]=dict(file=name,sha256=hashlib.sha256(body.encode()).hexdigest(),
                row_count=len(batch['rows']),min_time=min(r['open_time'] for r in batch['rows']),max_time=max(r['open_time'] for r in batch['rows']))
        (market/'raw_manifest.json').write_text(json.dumps(dict(strategy_id='hrs',queries=queries)))
        options=['--raw-market-export',str(market)]
    monitor=bootstrap['monitoring_model']
    code=main(['--sources',descriptor['path'],'--output-root',str(tmp_path/'output'),
        '--window-start',AT.isoformat(),'--as-of',(AT+timedelta(hours=3)).isoformat(),
        '--participation-rate',str(bootstrap['execution_model']['participation_rate']),
        '--monitor-cadence-seconds',str(monitor['cadence_seconds']),'--monitor-anchor',monitor['anchor_at'],
        '--allow-inception-segment',*options])
    assert code==0


def test_delayed_published_settlement_is_exported_at_actual_receipt(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from tests.test_ai_tuner_mvp.replay.test_hrs_replay_review import funding_rows
    from ai_tuner.replay.data_portal import DataPortal
    sources,bootstrap=captured_sources(tmp_path)
    original=DataPortal(tmp_path/'hrs','hrs',AT+timedelta(hours=3),AT)
    funded=funding_rows(original,available_offset=90)
    sources['funding_observations']=json.loads((funded.snapshot_dir/'funding_schedule_evidence.json').read_text())['observations']
    economic=AT+timedelta(hours=1,minutes=30);receipt=economic+timedelta(seconds=90)
    sources['decision_evidence'].append(envelope('funding','public_market_response',dict(path='/fapi/v1/fundingRate',params={'symbol':'A'},response=[dict(symbol='A',fundingTime=int(economic.timestamp()*1000),fundingRate='.01',markPrice='150')]),receipt,'A'))
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    row=json.loads((Path(result['snapshot_dir'])/'funding_rates.jsonl').read_text())
    assert row['mark_available_at']=='2026-11-17T01:31:30Z'
    assert row['funding_time']=='2026-11-17T01:30:00Z'


def test_other_strategy_inventory_is_an_explicit_policy_gap(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    for row in sources['decision_evidence']:
        if row['kind']=='scan_start':
            row['payload']['financial_context']['exchange_positions']=[dict(symbol='BTCUSDT',positionAmt='.1',entryPrice='100000')]
            row['payload_sha256']=canonical_payload(row['payload'])[1]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'])
    assert result['decision']=='BLOCKED_DATA'
    assert any('exogenous_inventory' in gap for gap in result['gaps'])


def test_retained_filled_reservation_cannot_be_ignored(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    row=next(r for r in sources['decision_evidence'] if r['kind']=='scan_start' and r['run_id']=='cycle-1')
    row['payload']['financial_context']['reservations']=[dict(state='submitting',symbol='A',quantity='10')]
    row['payload_sha256']=canonical_payload(row['payload'])[1]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'])
    assert result['decision']=='BLOCKED_DATA'
    assert any('retained_reservation' in gap for gap in result['gaps'])


def test_cashflow_verified_label_without_retained_ledger_proof_is_not_enough(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    at=AT+timedelta(hours=1)
    sources['cash_flows']=[dict(event_id='invented-deposit',segment_id='test',amount='1000',occurred_at=at.isoformat(),observed_at=at.isoformat(),verified=True)]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'])
    assert result['decision']=='BLOCKED_DATA'
    assert any('cashflow_ledger_proof_missing' in gap for gap in result['gaps'])


def test_cashflow_with_contemporaneous_retained_ledger_source_is_emitted(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    at=AT+timedelta(hours=1)
    flow=dict(event_id='allocation:journal:9',segment_id='test',amount='1000',occurred_at=at.isoformat(),nav_before='1',valuation_evidence='retained-proof')
    for row in sources['decision_evidence']:
        if row['kind']=='scan_start' and row['run_id'].startswith('cycle-'):
            finance=row['payload']['financial_context']
            finance.update(account_equity='11000',hrs_marked_equity='11000',allocation='11000')
            finance['ledger_state']['ledger_state'].update(cash_allocation='11000',equity='11000',units='11000',cash_flows=[flow])
            row['payload_sha256']=canonical_payload(row['payload'])[1]
    sources['cash_flows']=[dict(flow,observed_at=at.isoformat(),verified=True)]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    exported=json.loads((Path(result['snapshot_dir'])/'hrs_cash_flows.jsonl').read_text())
    assert exported['event_id']==flow['event_id'] and exported['observed_at']=='2026-11-17T01:00:00Z'


def test_complete_other_position_ownership_exports_actual_global_slot_rejection(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    symbols=['BTCUSDT','BNBUSDT','ETHUSDT']
    inventory=[dict(symbol=s,positionAmt='1',entryPrice='100',markPrice='100',leverage='2') for s in symbols]
    fills=[dict(account_id='test',symbol=s,trade_id=i,order_id=i,qty='1',price='100',side='BUY',trade_time_utc=(AT-timedelta(hours=1)).isoformat()) for i,s in enumerate(symbols,1)]
    owners=[dict(account_id='test',symbol=s,order_id=i,strategy='btc_eth') for i,s in enumerate(symbols,1)]
    for row in sources['decision_evidence']:
        if row['kind']!='scan_start':continue
        finance=row['payload']['financial_context']
        finance.update(exchange_positions=inventory,independent_positions=[dict(symbol=p['symbol'],positionAmt=p['positionAmt']) for p in inventory],
            claims=[dict(symbol=s,strategy='btc_eth',claim_state='ACTIVE',expires_at=(AT+timedelta(days=1)).isoformat()) for s in symbols])
        row['payload_sha256']=canonical_payload(row['payload'])[1]
        at=finance['available_at']
        proof=dict(account_id='test',segment_id='test',as_of=at,available_at=at,capture=dict(local={},inventory=inventory,independent=finance['independent_positions']),
            fills=fills,order_owners=owners,source_collection=dict(complete=True,gaps=[]),income_report=dict(id_match_and_coverage_complete=True,events=[],coverage_gaps=[]),
            allocation_audit=dict(version=1,journal=[]))
        sources['equity_evidence'].append(dict(payload=proof,evidence_sha256=canonical_payload(proof)[1]))
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    row=json.loads((Path(result['snapshot_dir'])/'hrs_decision_inputs.jsonl').read_text().splitlines()[0])
    assert len(row['risk']['account_occupancy']['other_positions'])==3


def test_sampled_ledger_component_retains_source_clock_and_age_distribution(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    for index,row in enumerate(r for r in sources['decision_evidence'] if r['kind']=='scan_start' and r['run_id'].startswith('cycle-')):
        finance=row['payload']['financial_context'];at=AT+timedelta(hours=index+1);source_at=at-timedelta(seconds=30)
        proof=dict(account_id='test',segment_id='test',as_of=source_at.isoformat(),available_at=(source_at+timedelta(seconds=1)).isoformat(),result=dict(ledger_version=index+2),source_collection=dict(complete=True,gaps=[]),income_report=dict(id_match_and_coverage_complete=True,events=[],coverage_gaps=[]),allocation_audit=dict(version=1,journal=[]))
        digest=canonical_payload(proof)[1];sources['equity_evidence'].append(dict(payload=proof,evidence_sha256=digest))
        finance.pop('hrs_marked_equity')
        finance['ledger_state']['version']=index+2
        finance['ledger_state']['ledger_state']['as_of']=source_at.isoformat()
        finance['hrs_marked_equity_observation']=dict(value='10000',as_of=source_at.isoformat(),available_at=proof['available_at'],evidence_sha256=digest,ledger_version=index+2)
        row['payload_sha256']=canonical_payload(row['payload'])[1]
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    receipt=json.loads((Path(result['snapshot_dir'])/'hrs_export_receipt.json').read_text())
    assert receipt['source_age_distribution']['max_seconds']==30
    assert receipt['mark_synchrony'] is False and receipt['exact_exchange_parity'] is False


def test_actual_stage_specific_capture_packets_are_combined_without_inventing_later_inputs(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=captured_sources(tmp_path)
    retained=[]
    for row in sources['decision_evidence']:
        if row['kind']!='entry_stage_inputs':retained.append(row);continue
        facts=row['payload'];at=AT+timedelta(hours=int(row['run_id'][-1])+1)
        stages=[dict(stage='exchange_position',exchange_positions=[]),dict(stage='claims',claims=[]),
            dict(stage='pool_circuit',pool_index=None,pool_index_12h=None),
            dict(stage='pre_entry_oi',pre_entry_oi_usd=facts['pre_entry_oi_usd']),
            dict(stage='pre_entry_funding',pre_entry_funding_rate=facts['pre_entry_funding_rate']),
            dict(stage='reservation_inventory',exchange_positions=[])]
        retained.extend(envelope(row['run_id'],'entry_stage_inputs',p,at,'A') for p in stages)
    sources['decision_evidence']=retained
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result


async def test_database_export_selects_referenced_equity_and_scanned_funding_only():
    from ai_tuner.hrs_evidence_export import read_evidence_sources
    class Transaction:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class DB:
        async def fetchval(self,*args):return None
        queries=[]
        def transaction(self,**kwargs):return Transaction()
        async def fetch(self,query,*args):
            self.queries.append((query,args))
            if 'FROM hrs.hrs_decision_evidence' in query:
                rows=[dict(kind='scan_start',payload=dict(financial_context=dict(available_at=AT.isoformat(),
                    ledger_state=dict(segment_id='test',version=5,ledger_state=dict(as_of=AT.isoformat())),
                    equity_source_archives=[dict(payload=dict(account_id='test'),evidence_sha256=canonical_payload(dict(account_id='test'))[1])]))),
                    dict(kind='standard_scan_scope',payload=dict(symbols=['A']))]
                for row in rows:row['payload_sha256']=canonical_payload(row['payload'])[1]
                return rows
            return []
    db=DB();await read_evidence_sources(db,'test',window_start=AT,as_of=AT+timedelta(hours=3))
    equity=next(query for query,_ in db.queries if 'FROM trading.hrs_equity_evidence' in query)
    funding=next(query for query,_ in db.queries if 'FROM public.ai_tuner_funding_schedule_observations' in query)
    assert 'evidence_sha256=ANY' in equity
    assert 'symbol=ANY' in funding


@pytest.mark.parametrize('archived',[False,True])
async def test_original_full_inventory_forecasts_fill_only_proven_missing_schedule_sources(tmp_path,archived):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from strategies.hrs.funding_inventory import FundingInventoryCollector
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter
    from ai_tuner.replay.data_portal import DataPortal
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from ai_tuner.replay.engine import run_replay
    from tests.test_ai_tuner_mvp.replay.test_hrs_wiring import broker
    sources,bootstrap=captured_sources(tmp_path)
    sources['source_account_id']='test';sources['funding_observations']=[]
    for row in sources['decision_evidence']:
        if row['kind'] not in ('scan_start','scan_end'):continue
        snapshot=row['payload'] if row['kind']=='scan_start' else row['payload']['state']
        snapshot['financial_context']['entry_policy']['account_id']='test'
        row['payload_sha256']=canonical_payload(row['payload'])[1]
    for index in range(37):
        requested=AT-timedelta(seconds=60)+timedelta(seconds=300*index)
        premium=[dict(symbol=symbol,time=int(requested.timestamp()*1000),nextFundingTime=int((AT+timedelta(hours=8)).timestamp()*1000)) for symbol in ('A','UNUSED')]
        api=SimpleNamespace(_request=AsyncMock(side_effect=[premium,[]]));recorder=SimpleNamespace(record=AsyncMock(),failed=False)
        collector=FundingInventoryCollector(api,recorder,account_id='test');times=iter(requested+timedelta(seconds=i) for i in range(4));collector.clock=lambda:next(times)
        await collector.collect_once();call=recorder.record.await_args
        sources['decision_evidence'].append(envelope('inventory-'+str(index),call.args[1],call.args[2],call.kwargs['as_of']))
        sources['decision_evidence'][-1]['available_at']=call.kwargs['available_at'].isoformat()
    original=copy.deepcopy(sources)
    if archived:
        with EvidenceArchiveWriter(tmp_path/'archive',source_metadata={}) as writer:
            for name,rows in sources.items():
                if not isinstance(rows,list):continue
                writer.declare_source(name,dict(expected_count=len(rows),query_scope='independent engineering inventory',account_id='test',window_start=(AT-timedelta(days=31)).isoformat(),window_end=(AT+timedelta(hours=3)).isoformat()))
                for row in rows:writer.write_row(name,row)
            descriptor=writer.finish()
        sources=dict(source_account_id='test',evidence_archive=descriptor)
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    path=Path(result['snapshot_dir']);portal=DataPortal(path,'hrs',AT+timedelta(hours=3),AT)
    schedule=portal.funding_schedule_evidence()
    if archived:
        assert len(schedule['observations'])==37
        assert {r['symbol'] for r in schedule['observations']}=={'A'}
        from ai_tuner.hrs_evidence_export import snapshot_evidence_sources
        stored=snapshot_evidence_sources(portal)
        assert stored.archive.counts['funding_observations']==0
        assert sum(1 for _ in stored.archive.iter_records('decision_evidence',kind='funding_schedule_inventory'))==37
    if not archived:
        assert sources==original
        assert {r['symbol'] for r in schedule['observations']}=={'A'}
    tables=json.loads((path/'hrs_state_tables.json').read_text())
    assert any(e['type']=='hrs_score' for e in run_replay(portal,HrsAdapter(lambda at:tables),broker()).audit_events)
    assert result['performance_qualified'] is False


async def test_inventory_sql_retains_only_original_account_bound_420_second_seeds():
    from ai_tuner.hrs_evidence_export import read_evidence_sources
    class Transaction:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class DB:
        async def fetchval(self,*args):return None
        def __init__(self):self.queries=[]
        def transaction(self,**options):
            assert options==dict(readonly=True,isolation='repeatable_read')
            return Transaction()
        async def fetch(self,sql,*args):self.queries.append((sql,args));return []
    db=DB();await read_evidence_sources(db,'test',window_start=AT,as_of=AT+timedelta(hours=3))
    sql,args=db.queries[0]
    assert "kind='funding_schedule_inventory'" in sql
    assert "payload->>'account_id'=$3" in sql and 'available_at <= $2' in sql
    assert args[3]==AT-timedelta(seconds=420)


def _guarded_sources(tmp_path):
    from shared.circuit_breaker import floor_index_hour
    from datetime import datetime
    sources,bootstrap=captured_sources(tmp_path)
    from shared.circuit_breaker import load_circuit_breaker_config
    config=load_circuit_breaker_config()
    for row in sources['decision_evidence']:
        if row['kind']!='scan_start':continue
        at=datetime.fromisoformat(row['event_time'])
        payload=row['payload'];payload['market_circuit_breaker_config']=dict(config)
        financial=payload['financial_context'];financial['entry_policy']['account_id']='test'
        financial['market_guard_observation']=dict(version='hrs_market_guard_observation_v1',account_id='test',pool='hrs',
            index_hour=(floor_index_hour(at)-timedelta(hours=1)).isoformat(),
            requested_at=(at-timedelta(seconds=2)).isoformat(),available_at=(at-timedelta(seconds=1)).isoformat(),
            config_sha256=canonical_payload(config)[1],pool_index=.001,pool_index_12h=None)
        key=financial['market_guard_observation']['index_hour']
        payload['market_circuit_breaker_state']['index_cache']={key:.001,key+':12h':None}
        row['payload_sha256']=canonical_payload(payload)[1]
    return sources,bootstrap


@pytest.mark.parametrize('problem',['account','config','pool','hour','future','bool','cache_missing','cache_mismatch'])
def test_market_guard_observation_requires_original_account_config_hour_and_clocks(tmp_path,problem):
    from ai_tuner.hrs_evidence_export import _market_guard_inputs,_records
    from ai_tuner.replay.models import DataBlocked
    sources,_=_guarded_sources(tmp_path)
    row=next(r for r in sources['decision_evidence'] if r['kind']=='scan_start' and r['run_id']=='cycle-0')
    observation=row['payload']['financial_context']['market_guard_observation']
    if problem=='account':observation['account_id']='foreign'
    elif problem=='config':observation['config_sha256']='0'*64
    elif problem=='pool':observation['pool']='mtpcs'
    elif problem=='hour':observation['index_hour']=row['event_time']
    elif problem=='future':observation['available_at']= (AT+timedelta(hours=1,seconds=1)).isoformat()
    elif problem=='cache_missing':row['payload']['market_circuit_breaker_state']['index_cache'].clear()
    elif problem=='cache_mismatch':row['payload']['market_circuit_breaker_state']['index_cache'][observation['index_hour']]=.04
    else:observation['pool_index']=True
    row['payload_sha256']=canonical_payload(row['payload'])[1]
    original=_records([row])[0]
    with pytest.raises(DataBlocked,match='market_guard'):
        _market_guard_inputs(original,original['event_time'],original['available_at'],[])


def test_pre_boundary_market_guard_observation_seals_and_rechecks_original_risk_inputs(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from ai_tuner.replay.data_portal import DataPortal
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from ai_tuner.replay.engine import run_replay
    from ai_tuner.replay.models import DataBlocked
    from tests.test_ai_tuner_mvp.replay.test_hrs_wiring import broker
    from tests.test_ai_tuner_mvp.replay.test_hrs_replay_review import refreeze,jsonl
    sources,bootstrap=_guarded_sources(tmp_path)
    result=export_evidence_bundle(sources,tmp_path/'output',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    path=Path(result['snapshot_dir']);portal=DataPortal(path,'hrs',AT+timedelta(hours=3),AT)
    inputs=[json.loads(line) for line in (path/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    assert inputs[0]['risk']['pool_index']==.001
    assert inputs[0]['risk']['pool_index_12h'] is None
    tables=json.loads((path/'hrs_state_tables.json').read_text())
    assert any(e['type']=='hrs_score' for e in run_replay(portal,HrsAdapter(lambda at:tables),broker()).audit_events)
    inputs[0]['risk']['pool_index']=.5
    from ai_tuner.replay.data_portal import compute_manifest_envelope_sha256,compute_manifest_sha256,sha256_file
    target=path/'hrs_decision_inputs.jsonl';target.write_text(jsonl(inputs))
    manifest_path=path/'manifest.json';manifest=json.loads(manifest_path.read_text())
    manifest['files'][target.name]['sha256']=sha256_file(str(target))
    manifest['sha256']=compute_manifest_sha256(manifest['files'])
    manifest['envelope_sha256']=compute_manifest_envelope_sha256(manifest)
    manifest_path.write_text(json.dumps(manifest));changed=DataPortal(path,'hrs',portal.as_of,portal.window_start)
    with pytest.raises(DataBlocked,match='market_guard'):
        run_replay(changed,HrsAdapter(lambda at:tables),broker())


@pytest.mark.parametrize('problem',[None,'hour','future','run'])
def test_original_per_entry_guard_receipt_overrides_only_its_proven_cycle_and_hour(tmp_path,problem):
    from ai_tuner.hrs_evidence_export import _market_guard_inputs,_records
    from ai_tuner.replay.models import DataBlocked
    sources,_=_guarded_sources(tmp_path)
    start=_records([next(r for r in sources['decision_evidence'] if r['kind']=='scan_start' and r['run_id']=='cycle-0')])[0]
    observation=start['payload']['financial_context']['market_guard_observation'];at=start['event_time']
    stage=envelope('cycle-0','entry_stage_inputs',dict(stage='pool_circuit',index_hour=observation['index_hour'],pool_index=None,pool_index_12h=.002),at+timedelta(seconds=1),'A')
    if problem=='hour':stage['payload']['index_hour']=at.isoformat()
    elif problem=='future':stage['event_time']=stage['available_at']=(at+timedelta(seconds=3)).isoformat()
    elif problem=='run':stage['run_id']='foreign-run'
    stage['payload_sha256']=canonical_payload(stage['payload'])[1];stage=_records([stage])[0]
    if problem:
        with pytest.raises(DataBlocked,match='market_guard'):_market_guard_inputs(start,at,at+timedelta(seconds=2),[stage])
    else:
        result=_market_guard_inputs(start,at,at+timedelta(seconds=2),[stage])
        assert result['pool_index'] is None and result['pool_index_12h']==.002
        assert result['market_guard_evidence']['source_evidence_id']==stage['evidence_id']


from tests.test_ai_tuner_mvp.test_hrs_equity_bridge_sources import source_database as inventory_source_database


async def test_readonly_inventory_seed_scope_uses_actual_availability_not_request_start(inventory_source_database):
    from ai_tuner.hrs_evidence_export import read_evidence_sources
    db=inventory_source_database
    for identity,account,requested,available in [
        ('eligible','test',AT-timedelta(seconds=450),AT-timedelta(seconds=399)),
        ('stale','test',AT-timedelta(seconds=450),AT-timedelta(seconds=421)),
        ('foreign','other',AT-timedelta(seconds=400),AT-timedelta(seconds=399)),
        ('future','test',AT+timedelta(hours=3),AT+timedelta(hours=3,seconds=1))]:
        payload=dict(account_id=account)
        await db.execute('INSERT INTO hrs.hrs_decision_evidence VALUES($1,$2,$3,NULL,$4,$5,$6,$7::jsonb)',
            identity,identity,'funding_schedule_inventory',requested,available,canonical_payload(payload)[1],json.dumps(payload))
    await db.execute('SET default_transaction_read_only=on')
    result=await read_evidence_sources(db,'test',window_start=AT,as_of=AT+timedelta(hours=3))
    assert [r['evidence_id'] for r in result['decision_evidence']]==['eligible']


@pytest.mark.parametrize('problem',['window_start','as_of','symbols','account_id','source_account','version','cached'])
async def test_archived_inventory_projection_scope_is_bound_to_sealed_portal_identity(tmp_path,problem):
    from ai_tuner.hrs_evidence_export import snapshot_evidence_sources
    from ai_tuner.replay.data_portal import DataPortal,compute_manifest_envelope_sha256,compute_manifest_sha256,sha256_file
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from ai_tuner.replay.engine import run_replay
    from ai_tuner.replay.models import DataBlocked
    # This exercises the actual exporter/archive reader/engine before damage.
    # There is no settlement in this fixture; scope drift alone is the defect.
    await test_original_full_inventory_forecasts_fill_only_proven_missing_schedule_sources(tmp_path,True)
    path=next((tmp_path/'output').rglob('raw_hrs_evidence.json')).parent
    portal=DataPortal(path,'hrs',AT+timedelta(hours=3),AT)
    if problem=='cached':
        sources=snapshot_evidence_sources(portal)
        sources.metadata['funding_inventory_scope']['window_start']=(AT+timedelta(seconds=70)).isoformat()
        try:
            with pytest.raises(DataBlocked,match='funding_inventory_scope'):
                snapshot_evidence_sources(portal)
        finally:
            from ai_tuner.hrs_evidence_export import close_portal_indexes
            close_portal_indexes(portal)
        return
    target=path/'raw_hrs_evidence.json';raw=json.loads(target.read_text())
    replacements=dict(window_start=(AT+timedelta(seconds=70)).isoformat(),
        as_of=(portal.as_of+timedelta(seconds=1)).isoformat(),symbols=['A','UNUSED'],
        account_id='foreign',version='unrecognized')
    if problem=='source_account':raw['source_account_id']='foreign'
    else:raw['funding_inventory_scope'][problem]=replacements[problem]
    target.write_text(json.dumps(raw))
    manifest_path=path/'manifest.json';manifest=json.loads(manifest_path.read_text())
    manifest['files'][target.name]['sha256']=sha256_file(str(target))
    manifest['sha256']=compute_manifest_sha256(manifest['files'])
    manifest['envelope_sha256']=compute_manifest_envelope_sha256(manifest)
    manifest_path.write_text(json.dumps(manifest))
    changed=DataPortal(path,'hrs',portal.as_of,portal.window_start)
    tables=json.loads((path/'hrs_state_tables.json').read_text())
    with pytest.raises(DataBlocked,match='funding_inventory_scope'):
        run_replay(changed,HrsAdapter(lambda at:tables),broker())
