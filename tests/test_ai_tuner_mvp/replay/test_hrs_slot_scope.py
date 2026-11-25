"""New slot policy needs original ownership facts; foreign margin still counts."""
import copy
import json
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from ai_tuner.replay.engine import run_replay
from ai_tuner.replay.models import DataBlocked
from strategies.hrs.equity_evidence import canonical,certify_owned_inventory_paths
from .test_hrs_wiring import AT,_hrs_fixture,broker


def ownership(at):
    inventory=[dict(symbol=s,positionAmt=q,entryPrice='10',markPrice='10',leverage='2')
        for s,q in [('WLDUSDT','28'),('BNBUSDT','.04'),('SOLUSDT','-.27')]]
    fills=[];owners=[];claims=[]
    for index,row in enumerate(inventory,1):
        owner='hrs' if index==1 else 'btc_eth_aggressive'
        fills.append(dict(account_id='test',symbol=row['symbol'],trade_id=index,order_id=index,
            side='BUY' if D(row['positionAmt'])>0 else 'SELL',qty=str(abs(D(row['positionAmt']))),
            price='10',trade_time_utc=at-timedelta(seconds=10)))
        owners.append(dict(account_id='test',symbol=row['symbol'],order_id=str(index),strategy=owner))
        claims.append(dict(id=index,symbol=row['symbol'],strategy=owner,claim_state='ACTIVE'))
    local={'WLDUSDT':dict(position_id='wld',direction='long',remaining_quantity='28')}
    paths=certify_owned_inventory_paths('test',{r['symbol']:D(r['positionAmt']) for r in inventory},fills,owners,coverage_complete=True)
    payload=dict(account_id='test',as_of=at.isoformat(),available_at=at.isoformat(),
        capture=dict(inventory=inventory,independent=inventory,local=local),fills=fills,order_owners=owners,
        source_collection=dict(complete=True,gaps=[],position_symbols_covered=[r['symbol'] for r in inventory]),
        inventory_owner_path_proofs=paths)
    encoded,digest=canonical(payload)
    return inventory,local,dict(payload=json.loads(encoded),sha256=digest),claims


def occupancy(at):
    from ai_tuner.hrs_evidence_export import _account_occupancy
    from strategies.hrs.entry_occupancy import SLOT_SCOPE,capture_hrs_slot_inputs
    inventory,local,parent,claims=ownership(at)
    inputs=capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local=local,observed_at=at)
    financial=dict(claims=claims,entry_policy=dict(account_id='test',revision=1),equity_gate=gate(parent,at))
    return _account_occupancy(inventory,dict(positions=local),financial,at,[parent],
        slot_scope=SLOT_SCOPE,slot_inputs=inputs,checked_at=at,
        source_evidence_id='inventory-source',claims_evidence_id='claims-source')


def test_scoped_export_proves_all_owners_and_keeps_foreign_inventory():
    row=occupancy(AT)
    assert row['version']=='hrs_account_occupancy_v2'
    assert row['slot_ownership_proof']['hrs_symbols']==['WLDUSDT']
    assert {p['symbol'] for p in row['other_positions']}=={'BNBUSDT','SOLUSDT'}
    assert row['slot_source_evidence_id']=='inventory-source'


def test_new_scope_uses_virtual_hrs_and_pending_slots_not_actual_baseline(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,initial_position=True,entry_type='LIMIT',subbars=True)
    b=broker();run_replay(portal,adapter,b)
    # Real baseline owns WLD, virtual candidate owns A: the actual baseline does
    # not add another slot to a counterfactual portfolio.
    risk=dict(account_occupancy=occupancy(portal.as_of))
    ctx=SimpleNamespace(portfolio=b.view())
    assert adapter._reservation_denial('NEW',D(1),D(100),ctx,portal.as_of,risk,D(10000)) is None
    adapter._durable_margin_reservations['PENDING']=dict(margin=D(10),terminal=False)
    assert adapter._reservation_denial('NEW',D(1),D(100),ctx,portal.as_of,risk,D(10000)) is None
    adapter._durable_margin_reservations['ANOTHER']=dict(margin=D(10),terminal=False)
    assert adapter._reservation_denial('NEW',D(1),D(100),ctx,portal.as_of,risk,D(10000))=='hrs_position_slots_include_retained_reservations'


def test_new_scope_retains_all_account_margin_and_rejects_foreign_add(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    b=broker();run_replay(portal,adapter,b)
    risk=dict(account_occupancy=occupancy(portal.as_of));ctx=SimpleNamespace(portfolio=b.view())
    assert adapter._reservation_denial('BNBUSDT',D(1),D(10),ctx,portal.as_of,risk,D(10000))=='account_position_already_exists'
    # Even without an HRS slot restriction, all-account/retained margin can deny.
    assert adapter._reservation_denial('NEW',D(1),D(100),ctx,portal.as_of,risk,D(1))=='entry_ceiling_includes_account_and_durable_reservations'


def test_new_scope_rejects_unproved_or_conflicting_owner_partition(tmp_path):
    portal,adapter,_=_hrs_fixture(tmp_path,entry_type='LIMIT',subbars=True)
    b=broker();run_replay(portal,adapter,b)
    row=occupancy(portal.as_of);row['slot_ownership_proof']['owners']['BNBUSDT']='hrs'
    with pytest.raises(DataBlocked,match='account_occupancy'):
        adapter._reservation_denial('NEW',D(1),D(100),SimpleNamespace(portfolio=b.view()),portal.as_of,dict(account_occupancy=row),D(10000))


def scoped_sources(tmp_path):
    from tests.test_ai_tuner_mvp.test_hrs_evidence_export import captured_sources
    from strategies.hrs.entry_occupancy import SLOT_SCOPE,capture_hrs_slot_inputs
    from ai_tuner.replay.models import parse_utc
    sources,bootstrap=captured_sources(tmp_path)
    for row in sources['decision_evidence']:
        if row['kind']!='scan_start':continue
        at=parse_utc(row['available_at']);inventory,_,parent,claims=ownership(at)
        payload=parent['payload'];local={}
        payload['capture']['local']=local
        for owner in payload['order_owners']:owner['strategy']='btc_eth_aggressive'
        for claim in claims:claim['strategy']='btc_eth_aggressive'
        fills=[dict(f,trade_time_utc=parse_utc(f['trade_time_utc'])) for f in payload['fills']]
        payload['inventory_owner_path_proofs']=certify_owned_inventory_paths('test',
            {p['symbol']:D(p['positionAmt']) for p in inventory},fills,payload['order_owners'],coverage_complete=True)
        financial=row['payload']['financial_context']
        financial['entry_policy']['account_id']='test'
        financial.update(runtime_slot_scope=SLOT_SCOPE,exchange_positions=inventory,independent_positions=inventory,claims=claims,ownership_claims=copy.deepcopy(claims),
            hrs_slot_inputs=capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local=local,observed_at=at))
        # JSON receipts contain actual UTC text, not Python-only datetimes.
        financial['hrs_slot_inputs']=json.loads(canonical(financial['hrs_slot_inputs'])[0])
        from strategies.hrs.decision_capture import canonical_payload
        row['payload_sha256']=canonical_payload(row['payload'])[1]
        payload.update(income_report=dict(id_match_and_coverage_complete=True,events=[],coverage_gaps=[]),
            allocation_audit=dict(version=1,journal=[]),result=dict(ready=True))
        financial['equity_gate']=gate(dict(payload=payload,sha256=canonical(payload)[1]),at)
        financial['equity_source_archives']=[dict(account_id='test',payload=copy.deepcopy(payload),evidence_sha256=canonical(payload)[1])]
        row['payload_sha256']=canonical_payload(row['payload'])[1]
        for old in sources['equity_evidence']:
            if old['payload']['as_of']==at.isoformat():old['payload'].update(copy.deepcopy(payload));old['evidence_sha256']=canonical(old['payload'])[1]
        sources['equity_evidence'].append(dict(payload=copy.deepcopy(payload),evidence_sha256=canonical(payload)[1]))
        for stage in sources['decision_evidence']:
            if stage['run_id']==row['run_id'] and stage['kind']=='entry_stage_inputs':
                stage['payload']['claims']=claims;stage['payload_sha256']=canonical_payload(stage['payload'])[1]
    return sources,bootstrap


def test_scoped_raw_sources_roundtrip_and_runtime_rechecks_original_parent(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from ai_tuner.replay.data_portal import DataPortal
    from pathlib import Path
    sources,bootstrap=scoped_sources(tmp_path)
    result=export_evidence_bundle(sources,tmp_path/'out',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result
    portal=DataPortal(result['snapshot_dir'],'hrs',AT+timedelta(hours=3),AT)
    states=json.loads((Path(result['snapshot_dir'])/'hrs_state_tables.json').read_text())
    replay=run_replay(portal,HrsAdapter(lambda at:states),broker())
    assert replay.final_position is not None
    # Re-sealing a derived proof is insufficient: raw owner paths must reproduce it.
    rows=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    rows[0]['risk']['account_occupancy']['slot_ownership_proof']['owners']['BNBUSDT']='new_coin'
    from ai_tuner.replay.data_portal import compute_manifest_envelope_sha256,compute_manifest_sha256,sha256_file
    file=portal.snapshot_dir/'hrs_decision_inputs.jsonl';file.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    manifest_path=portal.snapshot_dir/'manifest.json';manifest=json.loads(manifest_path.read_text())
    manifest['files'][file.name]['sha256']=sha256_file(str(file))
    manifest['sha256']=compute_manifest_sha256(manifest['files'])
    manifest['envelope_sha256']=compute_manifest_envelope_sha256(manifest)
    manifest_path.write_text(json.dumps(manifest))
    portal=DataPortal(portal.snapshot_dir,'hrs',portal.as_of,AT)
    with pytest.raises(DataBlocked,match='hrs_slot_raw_proof_mismatch'):
        run_replay(portal,HrsAdapter(lambda at:states),broker())


def test_locked_reservation_and_denial_parent_references_are_exported():
    from ai_tuner.hrs_evidence_export import _source_references
    from strategies.hrs.decision_capture import canonical_payload
    referenced=set()
    payload=dict(state=dict(financial_context=dict(equity_gate=dict(evidence_sha256='d'*64),reservations=[dict(geometry=json.dumps(
        dict(slot_occupancy_proof=dict(equity_evidence_sha256='b'*64))))])))
    _source_references(dict(kind='scan_end',payload=payload,payload_sha256=canonical_payload(payload)[1]),referenced,set(),set())
    denied=dict(stage='reservation_denial',risk_context=dict(evidence_sha256='c'*64))
    _source_references(dict(kind='entry_stage_inputs',payload=denied,payload_sha256=canonical_payload(denied)[1]),referenced,set(),set())
    success=dict(stage='reservation_success',slot_context=dict(evidence_sha256='e'*64),slot_occupancy_proof=dict(equity_evidence_sha256='e'*64))
    _source_references(dict(kind='entry_stage_inputs',payload=success,payload_sha256=canonical_payload(success)[1]),referenced,set(),set())
    assert referenced=={'b'*64,'c'*64,'d'*64,'e'*64}


def test_new_scope_cannot_certify_current_account_from_valid_foreign_parent():
    from ai_tuner.hrs_evidence_export import _account_occupancy
    from strategies.hrs.entry_occupancy import SLOT_SCOPE,capture_hrs_slot_inputs
    from ai_tuner.replay.models import parse_utc
    inventory,local,parent,claims=ownership(AT)
    payload=parent['payload'];payload['account_id']='foreign'
    for row in payload['fills']+payload['order_owners']:row['account_id']='foreign'
    fills=[dict(row,trade_time_utc=parse_utc(row['trade_time_utc'])) for row in payload['fills']]
    payload['inventory_owner_path_proofs']=certify_owned_inventory_paths('foreign',
        {p['symbol']:D(p['positionAmt']) for p in inventory},fills,payload['order_owners'],coverage_complete=True)
    parent['sha256']=canonical(payload)[1]
    inputs=capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local=local,observed_at=AT)
    with pytest.raises(DataBlocked,match='account_mismatch'):
        _account_occupancy(inventory,dict(positions=local),dict(claims=claims,entry_policy=dict(account_id='test',revision=1),equity_gate=gate(parent,AT)),AT,[parent],
            slot_scope=SLOT_SCOPE,slot_inputs=inputs,checked_at=AT,source_evidence_id='s',claims_evidence_id='c')


@pytest.mark.parametrize('maximum',[4,True,'3',0])
def test_new_scope_rejects_limits_outside_live_typed_one_to_three(tmp_path,maximum):
    portal,adapter,_=_hrs_fixture(tmp_path,initial_position=True,entry_type='LIMIT',subbars=True)
    b=broker();run_replay(portal,adapter,b)
    adapter.config.setdefault('trading',{})['max_positions']=maximum
    adapter._durable_margin_reservations.update(P1=dict(margin=D(1),terminal=False),P2=dict(margin=D(1),terminal=False))
    with pytest.raises(DataBlocked,match='hrs_slot_limit_invalid'):
        adapter._reservation_denial('NEW',D(1),D(100),SimpleNamespace(portfolio=b.view()),portal.as_of,
            dict(account_occupancy=occupancy(portal.as_of)),D(10000))


def gate(parent,checked):
    return dict(account_id='test',strategy_id='hrs',policy_revision=1,
        evidence_sha256=parent['sha256'],as_of=parent['payload']['as_of'],checked_at=checked.isoformat())


@pytest.mark.parametrize('selected',['new','old','missing','unknown'])
def test_gate_identity_never_falls_back_to_an_older_passable_parent(selected):
    from ai_tuner.hrs_evidence_export import _account_occupancy
    from strategies.hrs.entry_occupancy import SLOT_SCOPE,capture_hrs_slot_inputs
    inventory,local,old,claims=ownership(AT)
    new=copy.deepcopy(old);new['payload'].update(as_of=(AT+timedelta(seconds=2)).isoformat(),available_at=(AT+timedelta(seconds=2)).isoformat())
    new['sha256']=canonical(new['payload'])[1]
    observed=AT+timedelta(seconds=1);checked=AT+timedelta(seconds=3)
    financial=dict(claims=claims,entry_policy=dict(account_id='test',revision=1))
    if selected!='missing':financial['equity_gate']=gate(new if selected=='new' else old,checked)
    if selected=='unknown':financial['equity_gate']['evidence_sha256']='f'*64
    inputs=capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local=local,observed_at=observed)
    def build():return _account_occupancy(inventory,dict(positions=local),financial,observed,[old,new],
        slot_scope=SLOT_SCOPE,slot_inputs=inputs,checked_at=checked,source_evidence_id='scan',claims_evidence_id='scan')
    if selected=='old':assert build()['slot_ownership_proof']['equity_evidence_sha256']==old['sha256']
    else:
        with pytest.raises(DataBlocked):build()


def test_actual_locked_denial_identity_overrides_earlier_financial_gate(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from tests.test_ai_tuner_mvp.test_hrs_evidence_export import envelope
    from strategies.hrs.decision_capture import canonical_payload
    from ai_tuner.replay.models import parse_utc
    sources,bootstrap=scoped_sources(tmp_path)
    first=next(row for row in sources['decision_evidence'] if row['run_id']=='cycle-0' and row['kind']=='scan_start')
    at=parse_utc(first['event_time']);parent=copy.deepcopy(first['payload']['financial_context']['equity_source_archives'][0])
    parent['payload'].update(as_of=(at+timedelta(seconds=2)).isoformat(),available_at=(at+timedelta(seconds=2)).isoformat())
    parent['evidence_sha256']=canonical(parent['payload'])[1];sources['equity_evidence'].append(parent)
    reference=gate(dict(payload=parent['payload'],sha256=parent['evidence_sha256']),at+timedelta(seconds=3))
    reference['ownership_claims']=first['payload']['financial_context']['ownership_claims']
    sources['decision_evidence'].append(envelope('cycle-0','entry_stage_inputs',
        dict(stage='reservation_denial',risk_context=reference),at+timedelta(seconds=3),'A'))
    result=export_evidence_bundle(sources,tmp_path/'out',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('decision')=='BLOCKED_DATA',result
    assert any('hrs_slot_source_stale_or_future' in reason for reason in result['gaps'])


def test_success_context_binds_full_locked_claims_and_original_gate(tmp_path):
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from tests.test_ai_tuner_mvp.test_hrs_evidence_export import envelope
    from strategies.hrs.decision_capture import canonical_payload
    from strategies.hrs.entry_occupancy import SLOT_SCOPE,capture_hrs_slot_inputs,prove_hrs_slot_ownership
    from ai_tuner.replay.models import parse_utc
    from ai_tuner.replay.data_portal import DataPortal
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from pathlib import Path
    sources,bootstrap=scoped_sources(tmp_path)
    first=next(row for row in sources['decision_evidence'] if row['run_id']=='cycle-0' and row['kind']=='scan_start')
    at=parse_utc(first['event_time']);financial=first['payload']['financial_context']
    parent=financial['equity_source_archives'][0];inventory=financial['exchange_positions']
    inputs=capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local={},observed_at=at+timedelta(seconds=1))
    context=gate(dict(payload=parent['payload'],sha256=parent['evidence_sha256']),at+timedelta(seconds=2))
    context['ownership_claims']=copy.deepcopy(financial['ownership_claims'])
    proof=prove_hrs_slot_ownership('test',parent,inputs,context['ownership_claims'],checked_at=at+timedelta(seconds=2))
    for row in sources['decision_evidence']:
        if row['run_id']=='cycle-0' and row['kind']=='entry_stage_inputs':
            row['payload']['claims']=[]  # Candidate-only query, not all account owners.
            row['payload_sha256']=canonical_payload(row['payload'])[1]
    sources['decision_evidence'] += [
        envelope('cycle-0','entry_stage_inputs',dict(stage='reservation_inventory',slot_scope=SLOT_SCOPE,
            slot_inputs=inputs,exchange_positions=inventory),at+timedelta(seconds=1),'A'),
        envelope('cycle-0','entry_stage_inputs',dict(stage='reservation_success',client_order_id='test-success',
            slot_context=context,slot_occupancy_proof=proof),at+timedelta(seconds=3),'A')]
    result=export_evidence_bundle(sources,tmp_path/'out',window_start=AT,as_of=AT+timedelta(hours=3),
        execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    assert result.get('status')=='SEALED',result.get('gaps',result)
    root=Path(result['snapshot_dir']);rows=[json.loads(line) for line in (root/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    ref=rows[0]['risk']['account_occupancy']['gate_reference']
    assert ref['source_kind']=='reservation_success' and ref['checked_at'].startswith((at+timedelta(seconds=2)).isoformat()[:19])
    assert rows[0]['risk']['account_occupancy']['slot_ownership_proof']==proof
    portal=DataPortal(root,'hrs',AT+timedelta(hours=3),AT)
    states=json.loads((root/'hrs_state_tables.json').read_text())
    replay=run_replay(portal,HrsAdapter(lambda at:states),broker())
    assert replay.final_position is not None
