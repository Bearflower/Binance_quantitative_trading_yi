"""Engineering source/virtual-path contracts; these fixtures are not live results."""
import copy,json
from datetime import datetime,timedelta
from decimal import Decimal as D
from pathlib import Path
import pytest
from strategies.hrs.equity_evidence import canonical
from tests.test_ai_tuner_mvp.replay.test_hrs_wiring import AT,broker
from tests.test_ai_tuner_mvp.test_hrs_evidence_export import captured_sources


def bound_sources(tmp_path,post=False):
    sources,bootstrap=captured_sources(tmp_path)
    segment='test';account='test'
    left=AT+timedelta(minutes=2) if post else AT-timedelta(minutes=4)
    right=left+timedelta(minutes=2,seconds=5);received=right+timedelta(seconds=10)
    bars=[[int((left+timedelta(minutes=i)).timestamp()*1000),'105','110','105','105','0',int((left+timedelta(minutes=i+1)).timestamp()*1000)-1] for i in range(2)]
    fills=[dict(account_id=account,symbol='A',trade_id=str(i),order_id=str(i),trade_time_utc=at.isoformat(),side=side,qty='100',price='100')
        for i,at,side in [(1,left-timedelta(seconds=10),'BUY'),(2,right+timedelta(seconds=5),'SELL')]]
    owners=[dict(account_id=account,symbol='A',order_id=str(i),strategy='hrs') for i in (1,2)]
    source=copy.deepcopy(sources['equity_evidence'][0]['payload']);source.update(account_id=account,segment_id=segment,as_of=right.isoformat(),available_at=right.isoformat(),result=dict(ledger_version=1),fills=fills[:1],order_owners=owners)
    source_sha=canonical(source)[1]
    bounds=[dict(as_of=(left+timedelta(minutes=i)).isoformat(),low_nav='1.05',high_nav='1.10',source_units='10000') for i in (1,2)]
    receipt=dict(kind='automatic_constant_mark_gap_v1',account_id=account,segment_id=segment,
        as_of=(received-timedelta(seconds=1)).isoformat(),available_at=received.isoformat(),source_evidence_sha256=source_sha,source_ledger_version=1,
        mark_gaps=[dict(left=left.isoformat(),right=right.isoformat(),bars={'A':bars})],
        http_receipts=[dict(source='/fapi/v1/markPriceKlines',params=dict(symbol='A',interval='1m',startTime=bars[0][0],endTime=bars[-1][6],limit=128),requested_at=(received-timedelta(seconds=1)).isoformat(),received_at=received.isoformat(),response_sha256=canonical(bars)[1])],conservative_bounds=bounds)
    digest=canonical(receipt)[1]
    top=dict(version='hrs_conservative_gap_bounds_v1',account_id=account,segment_id=segment,available_at=received.isoformat(),
        receipts=[dict(receipt_sha256=digest,available_at=received.isoformat(),receipt=receipt)],
        bounds=[dict(bound,receipt_sha256=digest,source_available_at=received.isoformat()) for bound in bounds])
    sources['equity_evidence'].append(dict(evidence_sha256=source_sha,payload=source))
    sources['equity_reconciliations']=[dict(account_id=account,segment_id=segment,after_state_version=2,
        as_of=receipt['as_of'],event_time=receipt['as_of'],available_at=receipt['available_at'],receipt_sha256=digest,
        evidence_id=digest,receipt=copy.deepcopy(receipt),after_state=dict(ledger={},gate={}))]
    for row in sources['decision_evidence']:
        if row['kind'] not in ('scan_start','scan_end'):continue
        snapshot=row['payload'] if row['kind']=='scan_start' else row['payload']['state']
        financial=snapshot['financial_context'];at=row['event_time']
        financial['entry_policy']['account_id']=account
        version=int((datetime.fromisoformat(at)-AT).total_seconds())+2
        financial['ledger_state']['version']=version
        financial['ledger_state']['ledger_state']['as_of']=at
        parent=copy.deepcopy(sources['equity_evidence'][0]['payload'])
        parent.update(account_id=account,segment_id=segment,as_of=at,available_at=at,result=dict(ready=True,ledger_version=version))
        parent['fills']=[fill for fill in fills if datetime.fromisoformat(fill['trade_time_utc'])<=datetime.fromisoformat(at)];parent['order_owners']=owners
        if not post or datetime.fromisoformat(at)>=received:
            projected=copy.deepcopy(top)
            endpoint_rows=[dict(segment_id=segment,as_of=point.isoformat(),equity=value,units='10000',nav=str(D(value)/10000)) for point,value in ((left,'10000'),(right,'10500'))]
            valuation=dict(ledger_state=copy.deepcopy(financial['ledger_state']['ledger_state']),checkpoint_endpoints=endpoint_rows,opening_inventory=[])
            valuation_sha=canonical(valuation)[1]
            projected.update(valuation=valuation,valuation_sha256=valuation_sha,valuation_available_at=at,available_at=at)
            projected['bounds']=[dict(bound,valuation_sha256=valuation_sha,valuation_available_at=at) for bound in projected['bounds']]
            parent['history_gap_bounds']=projected
        parent_sha=canonical(parent)[1]
        if row['kind']=='scan_start':sources['equity_evidence'].append(dict(evidence_sha256=parent_sha,payload=parent))
        financial['hrs_marked_equity_observation']=dict(value='10000',ledger_version=version,as_of=at,available_at=at,evidence_sha256=parent_sha)
        row['payload_sha256']=canonical(row['payload'])[1]
    return sources,bootstrap


def test_original_receipt_bounds_project_with_hashes_and_actual_availability(tmp_path):
    from ai_tuner.hrs_evidence_export import _equity,project_history_gap_bounds
    sources,_=bound_sources(tmp_path)
    financial=sources['decision_evidence'][0]['payload']['financial_context']
    projection=project_history_gap_bounds(financial,_equity(sources['equity_evidence']),AT,sources['equity_reconciliations'])
    assert projection['bounds'][0]['high_nav']=='1.10'
    assert projection['parent_evidence_sha256']==financial['hrs_marked_equity_observation']['evidence_sha256']


@pytest.mark.parametrize('post',[False,True])
@pytest.mark.parametrize('archived',[False,True])
def test_original_bounds_roundtrip_actual_exporter_and_counterfactual_engine(tmp_path,post,archived):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from ai_tuner.replay.data_portal import DataPortal
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from ai_tuner.replay.engine import run_replay
    sources,bootstrap=bound_sources(tmp_path,post)
    if archived:
        from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter
        with EvidenceArchiveWriter(tmp_path/'archive',source_metadata={}) as writer:
            for name,rows in sources.items():
                writer.declare_source(name,dict(expected_count=len(rows),query_scope='bounded engineering gap bounds',account_id='test',window_start=(AT-timedelta(days=31)).isoformat(),window_end=(AT+timedelta(hours=3)).isoformat()))
                for row in rows:writer.write_row(name,row)
            descriptor=writer.finish()
        sources=dict(evidence_archive=descriptor)
    result=export_evidence_bundle(sources,tmp_path/'out',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    path=Path(result['snapshot_dir']);portal=DataPortal(path,'hrs',AT+timedelta(hours=3),AT)
    state=json.loads((path/'hrs_state_tables.json').read_text());adapter=HrsAdapter(lambda at:state)
    replay=run_replay(portal,adapter,broker())
    # Reusing a verified portal opens fresh owned readers after engine cleanup.
    adapter=HrsAdapter(lambda at:state)
    replay=run_replay(portal,adapter,broker())
    if post:
        assert any(event['type']=='hrs_actual_gap_bounds_ignored' for event in replay.audit_events)
    else:
        assert adapter._gate.rolling_latched
        assert not replay.final_position


@pytest.mark.parametrize('damage',['receipt','valuation_ledger','endpoint_units','late_valuation','account','source_clock'])
def test_original_source_semantics_cannot_be_upgraded_by_new_hashes(tmp_path,damage):
    from ai_tuner.hrs_evidence_export import _equity,project_history_gap_bounds
    from ai_tuner.replay.models import DataBlocked
    sources,_=bound_sources(tmp_path);changed={}
    for row in sources['equity_evidence']:
        parent=row['payload'];top=parent.get('history_gap_bounds')
        if not top:continue
        if damage=='receipt':top['receipts'][0]['receipt']['mark_gaps'][0]['bars']['A'][0][4]='999'
        elif damage=='valuation_ledger':top['valuation']['ledger_state']['cash_allocation']='10001'
        elif damage=='endpoint_units':
            for endpoint in top['valuation']['checkpoint_endpoints']:
                endpoint['units']='9999';endpoint['nav']=str(D(endpoint['equity'])/9999)
        elif damage=='late_valuation':top['valuation_available_at']=(datetime.fromisoformat(parent['available_at'])+timedelta(seconds=1)).isoformat()
        elif damage=='account':top['account_id']='foreign'
        else:
            ref=top['receipts'][0];ref['receipt']['http_receipts'][0]['requested_at']=(datetime.fromisoformat(ref['receipt']['mark_gaps'][0]['right'])-timedelta(seconds=1)).isoformat()
            ref['receipt_sha256']=canonical(ref['receipt'])[1]
            for bound in top['bounds']:bound['receipt_sha256']=ref['receipt_sha256']
        top['valuation_sha256']=canonical(top['valuation'])[1]
        for bound in top['bounds']:
            bound['valuation_sha256']=top['valuation_sha256'];bound['valuation_available_at']=top['valuation_available_at']
        old=row['evidence_sha256'];row['evidence_sha256']=canonical(parent)[1];changed[old]=row['evidence_sha256']
    financial=sources['decision_evidence'][0]['payload']['financial_context']
    financial['hrs_marked_equity_observation']['evidence_sha256']=changed[financial['hrs_marked_equity_observation']['evidence_sha256']]
    with pytest.raises(DataBlocked,match='history_gap'):
        project_history_gap_bounds(financial,_equity(sources['equity_evidence']),AT,sources['equity_reconciliations'])


@pytest.mark.parametrize('damage',['hourly','missing_minute','missing_funding'])
def test_actual_only_post_start_bounds_require_independent_candidate_coverage(tmp_path,damage):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=bound_sources(tmp_path,True)
    if damage=='hourly':
        for row in sources['decision_evidence']:
            if row['kind'] not in ('scan_start','scan_end'):continue
            snapshot=row['payload'] if row['kind']=='scan_start' else row['payload']['state']
            snapshot['config']['trading']['order_type']['entry']='MARKET'
            row['payload_sha256']=canonical(row['payload'])[1]
    elif damage=='missing_minute':
        batch=next(row for row in sources['market_bars'] if row['interval']=='1m')
        batch['rows'].pop(70);batch['payload_sha256']=canonical(batch['rows'])[1]
    else:sources['funding_observations']=[]
    result=export_evidence_bundle(sources,tmp_path/'out',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('decision')=='BLOCKED_DATA',result
    assert not list((tmp_path/'out').rglob('manifest.json'))


def test_bound_projection_tampering_after_normal_reseal_is_denied(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from ai_tuner.replay.data_portal import DataPortal,compute_manifest_sha256,compute_manifest_envelope_sha256,sha256_file
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from ai_tuner.replay.engine import run_replay
    from ai_tuner.replay.models import DataBlocked
    sources,bootstrap=bound_sources(tmp_path)
    result=export_evidence_bundle(sources,tmp_path/'out',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    path=Path(result['snapshot_dir']);target=path/'hrs_replay_state.json'
    raw=json.loads(target.read_text());raw['history_gap_bounds']['bounds'][0]['high_nav']='1'
    target.write_text(json.dumps(raw));manifest_path=path/'manifest.json';manifest=json.loads(manifest_path.read_text())
    manifest['files'][target.name]['sha256']=sha256_file(str(target));manifest['sha256']=compute_manifest_sha256(manifest['files'])
    manifest['envelope_sha256']=compute_manifest_envelope_sha256(manifest);manifest_path.write_text(json.dumps(manifest))
    portal=DataPortal(path,'hrs',AT+timedelta(hours=3),AT)
    tables=json.loads((path/'hrs_state_tables.json').read_text())
    with pytest.raises(DataBlocked,match='history_gap_bounds'):
        run_replay(portal,HrsAdapter(lambda at:tables),broker())


from tests.test_ai_tuner_mvp.test_hrs_equity_bridge_sources import source_database


@pytest.mark.parametrize('archive',[False,True])
@pytest.mark.parametrize('damage',[None,'missing','hash','version','missing_projection'])
async def test_readonly_sql_retains_old_gap_receipt_parents_and_their_forecast_prefix(source_database,tmp_path,archive,damage):
    from ai_tuner.hrs_evidence_export import read_evidence_sources,_ArchiveSources,_equity,project_history_gap_bounds,verify_forecast_source_closure,_records
    from ai_tuner.replay.models import DataBlocked
    from strategies.hrs.funding_reference import build_forecasts_ref
    from tests.test_ai_tuner_mvp.test_hrs_forecast_closure import observation
    sources,_=bound_sources(tmp_path);db=source_database
    origin=sources['equity_evidence'][1];old_origin=origin['evidence_sha256']
    cutoff=datetime.fromisoformat(origin['payload']['available_at'])
    forecast=observation(cutoff-timedelta(seconds=1));ref,_=build_forecasts_ref('test',cutoff,[forecast],expected_count=1)
    origin['payload'].setdefault('capture',{})['funding_publication']=dict(forecasts_ref=ref)
    origin['evidence_sha256']=canonical(origin['payload'])[1];replaced={old_origin:origin['evidence_sha256']}
    for row in sources['equity_evidence']:
        top=row['payload'].get('history_gap_bounds')
        if not top:continue
        old=row['evidence_sha256'];r=top['receipts'][0]
        r['receipt']['source_evidence_sha256']=origin['evidence_sha256'];r['receipt_sha256']=canonical(r['receipt'])[1]
        for bound in top['bounds']:bound['receipt_sha256']=r['receipt_sha256']
        if damage=='missing_projection':row['payload'].pop('history_gap_bounds')
        row['evidence_sha256']=canonical(row['payload'])[1];replaced[old]=row['evidence_sha256']
    independent=sources['equity_reconciliations'][0]
    independent['receipt']['source_evidence_sha256']=origin['evidence_sha256']
    if damage=='version':
        independent['receipt']['source_ledger_version']=998;independent['after_state_version']=999
    independent['receipt_sha256']=independent['evidence_id']=canonical(independent['receipt'])[1]
    await db.execute('INSERT INTO trading.hrs_equity_reconciliation VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb)',
        independent['account_id'],independent['segment_id'],independent['after_state_version'],datetime.fromisoformat(independent['as_of']),datetime.fromisoformat(independent['available_at']),independent['receipt_sha256'],json.dumps(independent['receipt']),json.dumps(independent['after_state']))
    for row in sources['decision_evidence']:
        if row['kind'] in ('scan_start','scan_end'):
            snapshot=row['payload'] if row['kind']=='scan_start' else row['payload']['state']
            component=snapshot['financial_context']['hrs_marked_equity_observation']
            component['evidence_sha256']=replaced[component['evidence_sha256']]
            row['payload_sha256']=canonical(row['payload'])[1]
        await db.execute('INSERT INTO hrs.hrs_decision_evidence VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)',
            row['evidence_id'],row['run_id'],row['kind'],row['symbol'],datetime.fromisoformat(row['event_time']),datetime.fromisoformat(row['available_at']),row['payload_sha256'],json.dumps(row['payload']))
    for row in sources['equity_evidence']:
        if row is origin and damage in ('missing','missing_projection'):continue
        payload=copy.deepcopy(row['payload'])
        if row is origin and damage=='hash':payload['damaged']=True
        await db.execute('INSERT INTO trading.hrs_equity_evidence VALUES($1,$2,$3,$4,$5,$6::jsonb)',
            'test',row['evidence_sha256'],'test',datetime.fromisoformat(payload['as_of']),datetime.fromisoformat(payload['available_at']),json.dumps(payload))
    await db.execute('INSERT INTO trading.hrs_funding_observation VALUES($1,$2,$3,$4::jsonb)',
        'test',forecast['evidence_sha256'],datetime.fromisoformat(forecast['available_at']),json.dumps(forecast['payload']))
    await db.execute('SET default_transaction_read_only=on')
    if damage:
        with pytest.raises(DataBlocked,match='history_gap_bounds_source_parent|equity_payload_hash_mismatch'):
            await read_evidence_sources(db,'test',window_start=AT,as_of=AT+timedelta(hours=3),archive_root=tmp_path/'archive' if archive else None)
        return
    result=await read_evidence_sources(db,'test',window_start=AT,as_of=AT+timedelta(hours=3),archive_root=tmp_path/'archive' if archive else None)
    if archive:
        with _ArchiveSources(result) as retained:
            rows=list(retained.archive.iter_records('equity_evidence'))
            receipts=list(retained.archive.iter_records('equity_reconciliations'))
            assert retained.archive.source_metadata['equity_evidence']['expected_count']==len(rows)
            assert retained.forecast_closure['source_count']==1
    else:
        rows=result['equity_evidence'];receipts=result['equity_reconciliations']
        assert verify_forecast_source_closure(result,_records(result['decision_evidence']))['source_count']==1
    original=next(row for row in rows if row['evidence_sha256']==origin['evidence_sha256'])
    assert (json.loads(original['payload']) if isinstance(original['payload'],str) else original['payload'])==origin['payload']
    assert original['available_at']==origin['payload']['available_at']
    financial=sources['decision_evidence'][0]['payload']['financial_context']
    assert project_history_gap_bounds(financial,_equity(rows),AT,receipts)['bounds']


@pytest.mark.parametrize('archived',[False,True])
@pytest.mark.parametrize('omission',['one_bound','all_bounds','both','missing_inventory','projection_paired_version'])
def test_independent_original_receipt_inventory_rejects_omitted_bounds(tmp_path,archived,omission):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    sources,bootstrap=bound_sources(tmp_path);changed={}
    for row in sources['equity_evidence']:
        top=row['payload'].get('history_gap_bounds')
        if not top:continue
        old=row['evidence_sha256']
        if omission=='one_bound':top['bounds'].pop()
        elif omission in ('all_bounds','both'):top['bounds']=[]
        if omission=='both':top['receipts']=[]
        if omission=='projection_paired_version':row['payload'].pop('history_gap_bounds')
        row['evidence_sha256']=canonical(row['payload'])[1];changed[old]=row['evidence_sha256']
    if omission=='missing_inventory':del sources['equity_reconciliations']
    elif omission=='projection_paired_version':
        independent=sources['equity_reconciliations'][0]
        independent['receipt']['source_ledger_version']=998;independent['after_state_version']=999
        independent['receipt_sha256']=independent['evidence_id']=canonical(independent['receipt'])[1]
    for row in sources['decision_evidence']:
        if row['kind'] not in ('scan_start','scan_end'):continue
        snapshot=row['payload'] if row['kind']=='scan_start' else row['payload']['state']
        component=snapshot['financial_context']['hrs_marked_equity_observation']
        component['evidence_sha256']=changed[component['evidence_sha256']]
        row['payload_sha256']=canonical(row['payload'])[1]
    if archived:
        from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter
        with EvidenceArchiveWriter(tmp_path/'archive',source_metadata={}) as writer:
            for name,rows in sources.items():
                writer.declare_source(name,dict(expected_count=len(rows),account_id='test',query_scope='original receipt inventory omission',window_start=(AT-timedelta(days=31)).isoformat(),window_end=(AT+timedelta(hours=3)).isoformat()))
                for row in rows:writer.write_row(name,row)
            descriptor=writer.finish()
        sources=dict(evidence_archive=descriptor)
    result=export_evidence_bundle(sources,tmp_path/'out',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('decision')=='BLOCKED_DATA',result
    assert any('history_gap_bounds' in reason for reason in result['gaps'])
    assert not list((tmp_path/'out').rglob('manifest.json'))


def test_aged_out_independent_receipt_does_not_require_an_inactive_projection(tmp_path):
    from ai_tuner.hrs_evidence_export import _equity,project_history_gap_bounds
    sources,_=bound_sources(tmp_path)
    financial=sources['decision_evidence'][0]['payload']['financial_context'];old=financial['hrs_marked_equity_observation']['evidence_sha256']
    parent=next(row for row in sources['equity_evidence'] if row['evidence_sha256']==old)
    later=AT+timedelta(days=32);parent['payload'].pop('history_gap_bounds')
    parent['payload']['as_of']=parent['payload']['available_at']=later.isoformat()
    parent['evidence_sha256']=canonical(parent['payload'])[1]
    financial['available_at']=later.isoformat();financial['ledger_state']['ledger_state']['as_of']=later.isoformat()
    financial['hrs_marked_equity_observation'].update(evidence_sha256=parent['evidence_sha256'],as_of=later.isoformat(),available_at=later.isoformat())
    assert project_history_gap_bounds(financial,_equity(sources['equity_evidence']),later,sources['equity_reconciliations']) is None


@pytest.mark.parametrize('damage',['version','clock','account','after_gate'])
def test_independent_receipt_metadata_cannot_hide_known_bounds(tmp_path,damage):
    from ai_tuner.hrs_evidence_export import _equity,project_history_gap_bounds
    from ai_tuner.replay.models import DataBlocked
    sources,_=bound_sources(tmp_path);financial=sources['decision_evidence'][0]['payload']['financial_context']
    parent=next(row for row in sources['equity_evidence'] if row['evidence_sha256']==financial['hrs_marked_equity_observation']['evidence_sha256'])
    parent['payload'].pop('history_gap_bounds');parent['evidence_sha256']=canonical(parent['payload'])[1]
    financial['hrs_marked_equity_observation']['evidence_sha256']=parent['evidence_sha256']
    row=sources['equity_reconciliations'][0]
    if damage=='version':row['after_state_version']=999
    elif damage=='clock':row['available_at']=(AT+timedelta(seconds=1)).isoformat()
    elif damage=='account':row['account_id']='foreign'
    else:
        row['receipt']['after_gate_sha256']='0'*64;row['receipt_sha256']=canonical(row['receipt'])[1]
    with pytest.raises(DataBlocked,match='history_gap_bounds'):
        project_history_gap_bounds(financial,_equity(sources['equity_evidence']),AT,sources['equity_reconciliations'])


@pytest.mark.parametrize('archived',[False,True])
@pytest.mark.parametrize('damage',['version','source_clock','missing_parent'])
def test_original_parent_binding_precedes_independent_receipt_causal_filter(tmp_path,archived,damage):
    from ai_tuner.hrs_evidence_export import _ArchiveSources,_EquityView,_equity,project_history_gap_bounds
    from ai_tuner.replay.models import DataBlocked
    sources,_=bound_sources(tmp_path)
    financial=sources['decision_evidence'][0]['payload']['financial_context']
    parent=next(row for row in sources['equity_evidence'] if row['evidence_sha256']==financial['hrs_marked_equity_observation']['evidence_sha256'])
    parent['payload'].pop('history_gap_bounds');parent['evidence_sha256']=canonical(parent['payload'])[1]
    financial['hrs_marked_equity_observation']['evidence_sha256']=parent['evidence_sha256']
    row=sources['equity_reconciliations'][0];receipt=row['receipt']
    origin=next(item for item in sources['equity_evidence'] if item['evidence_sha256']==receipt['source_evidence_sha256'])
    if damage=='version':
        receipt['source_ledger_version']=998;row['after_state_version']=999
    else:
        row['available_at']=receipt['available_at']=(AT+timedelta(seconds=1)).isoformat()
        if damage=='source_clock':receipt['http_receipts'][0]['requested_at']=(datetime.fromisoformat(origin['payload']['available_at'])-timedelta(seconds=1)).isoformat()
        else:sources['equity_evidence'].remove(origin)
    row['receipt_sha256']=row['evidence_id']=canonical(receipt)[1]
    sources['decision_evidence'][0]['payload_sha256']=canonical(sources['decision_evidence'][0]['payload'])[1]
    with pytest.raises(DataBlocked,match='history_gap_bounds_source_|equity_payload_hash_mismatch'):
        if archived:
            from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter
            with EvidenceArchiveWriter(tmp_path/'archive',source_metadata={}) as writer:
                for name,rows in sources.items():
                    writer.declare_source(name,dict(expected_count=len(rows),account_id='test',query_scope='engineering receipt parent binding',window_start=(AT-timedelta(days=31)).isoformat(),window_end=(AT+timedelta(hours=3)).isoformat()))
                    for item in rows:writer.write_row(name,item)
                descriptor=writer.finish()
            with _ArchiveSources(dict(evidence_archive=descriptor)) as retained:
                project_history_gap_bounds(financial,_EquityView(retained),AT,retained.get('equity_reconciliations'))
        else:
            project_history_gap_bounds(financial,_equity(sources['equity_evidence']),AT,sources['equity_reconciliations'])


@pytest.mark.parametrize('archived',[False,True])
@pytest.mark.parametrize('case',['future_version','future_available','different_segment','legacy'])
def test_consistent_irrelevant_receipts_preserve_no_gap_projection(tmp_path,case,archived):
    from ai_tuner.hrs_evidence_export import _ArchiveSources,_EquityView,_equity,project_history_gap_bounds
    sources,_=bound_sources(tmp_path)
    financial=sources['decision_evidence'][0]['payload']['financial_context']
    parent=next(row for row in sources['equity_evidence'] if row['evidence_sha256']==financial['hrs_marked_equity_observation']['evidence_sha256'])
    parent['payload'].pop('history_gap_bounds');parent['evidence_sha256']=canonical(parent['payload'])[1]
    financial['hrs_marked_equity_observation']['evidence_sha256']=parent['evidence_sha256']
    row=sources['equity_reconciliations'][0];receipt=row['receipt']
    origin=next(item for item in sources['equity_evidence'] if item['evidence_sha256']==receipt['source_evidence_sha256'])
    if case=='future_version':
        receipt['source_ledger_version']=origin['payload']['result']['ledger_version']=998;row['after_state_version']=999
    elif case=='future_available':row['available_at']=receipt['available_at']=(AT+timedelta(seconds=1)).isoformat()
    elif case=='different_segment':row['segment_id']=receipt['segment_id']=origin['payload']['segment_id']='another'
    else:sources['equity_reconciliations']=[]
    origin['evidence_sha256']=canonical(origin['payload'])[1];receipt['source_evidence_sha256']=origin['evidence_sha256']
    row['receipt_sha256']=row['evidence_id']=canonical(receipt)[1]
    if archived:
        from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter
        sources['decision_evidence'][0]['payload_sha256']=canonical(sources['decision_evidence'][0]['payload'])[1]
        with EvidenceArchiveWriter(tmp_path/'archive',source_metadata={}) as writer:
            for name,rows in sources.items():
                writer.declare_source(name,dict(expected_count=len(rows),account_id='test',query_scope='engineering irrelevant receipt',window_start=(AT-timedelta(days=31)).isoformat(),window_end=(AT+timedelta(hours=3)).isoformat()))
                for item in rows:writer.write_row(name,item)
            descriptor=writer.finish()
        with _ArchiveSources(dict(evidence_archive=descriptor)) as retained:
            assert project_history_gap_bounds(financial,_EquityView(retained),AT,retained.get('equity_reconciliations')) is None
    else:assert project_history_gap_bounds(financial,_equity(sources['equity_evidence']),AT,sources['equity_reconciliations']) is None


@pytest.mark.parametrize('empty',[False,True])
def test_original_receipt_archive_binding_does_not_require_decision_family(tmp_path,empty):
    from ai_tuner.hrs_evidence_export import _ArchiveSources
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter
    sources,_=bound_sources(tmp_path)
    with EvidenceArchiveWriter(tmp_path/'archive',source_metadata={}) as writer:
        for name in ('equity_evidence','equity_reconciliations'):
            rows=[] if empty else sources[name]
            writer.declare_source(name,dict(expected_count=len(rows),account_id='test',query_scope='engineering receipt closure without decisions',window_start=(AT-timedelta(days=31)).isoformat(),window_end=(AT+timedelta(hours=3)).isoformat()))
            for row in rows:writer.write_row(name,row)
        descriptor=writer.finish()
    with _ArchiveSources(dict(evidence_archive=descriptor)) as retained:
        assert len(retained.get('equity_reconciliations'))==(0 if empty else 1)
