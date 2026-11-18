"""Raw-capture conversion contracts; engineering fixtures are never live evidence."""
import copy
import json
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
        queries=[]
        def transaction(self,**kwargs):return Transaction()
        async def fetch(self,query,*args):
            self.queries.append((query,args))
            if 'FROM hrs.hrs_decision_evidence' in query:
                return [dict(kind='scan_start',payload=dict(financial_context=dict(available_at=AT.isoformat(),
                    ledger_state=dict(segment_id='test',version=5,ledger_state=dict(as_of=AT.isoformat())),
                    equity_source_archives=[dict(evidence_sha256='a'*64)]))),
                    dict(kind='standard_scan_scope',payload=dict(symbols=['A']))]
            return []
    db=DB();await read_evidence_sources(db,'test',window_start=AT,as_of=AT+timedelta(hours=3))
    equity=next(query for query,_ in db.queries if 'FROM trading.hrs_equity_evidence' in query)
    funding=next(query for query,_ in db.queries if 'FROM public.ai_tuner_funding_schedule_observations' in query)
    assert 'evidence_sha256=ANY' in equity
    assert 'symbol=ANY' in funding
