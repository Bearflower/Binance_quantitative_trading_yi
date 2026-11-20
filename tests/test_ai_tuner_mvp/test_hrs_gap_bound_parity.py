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
    projection=project_history_gap_bounds(financial,_equity(sources['equity_evidence']),AT)
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
        project_history_gap_bounds(financial,_equity(sources['equity_evidence']),AT)


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
