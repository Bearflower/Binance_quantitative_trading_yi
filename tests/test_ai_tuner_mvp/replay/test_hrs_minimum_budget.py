"""Sealed synthetic replay contracts; these fixtures prove no performance claim."""
import json
from decimal import Decimal as D

import pytest

from ai_tuner.replay.engine import run_replay
from ai_tuner.replay.models import DataBlocked
from strategies.hrs.entry_occupancy import SLOT_SCOPE, capture_hrs_slot_inputs
from strategies.hrs.equity_evidence import canonical, certify_owned_inventory_paths
from strategies.hrs.risk_budget import FLOOR
from .test_hrs_wiring import _hrs_fixture, broker
from .test_hrs_replay_review import refreeze, jsonl


def floor_fixture(tmp_path, *, version=FLOOR, allocation='4', ceiling='4', foreign_margin='0', margin_scope=None, cash='448.86', source_transform=None, export_result=False):
    from datetime import timedelta
    from pathlib import Path
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from ai_tuner.replay.adapters.hrs import HrsAdapter
    from ai_tuner.replay.data_portal import DataPortal
    from ai_tuner.replay.models import parse_utc
    from strategies.hrs.decision_capture import canonical_payload
    from tests.test_ai_tuner_mvp.test_hrs_evidence_export import captured_sources
    from .test_hrs_wiring import AT
    from .test_hrs_slot_scope import gate
    sources, bootstrap = captured_sources(tmp_path)
    for row in sources['decision_evidence']:
        if row['kind'] != 'scan_start':
            continue
        at = row['available_at']
        inventory = [] if D(foreign_margin)==0 else [dict(symbol='F',positionAmt='1',entryPrice=str(D(foreign_margin)*2),markPrice=str(D(foreign_margin)*2),leverage='2')]
        fills = [] if not inventory else [dict(account_id='test',symbol='F',trade_id=1,order_id=1,side='BUY',qty='1',price=inventory[0]['entryPrice'],trade_time_utc=at)]
        owners = [] if not inventory else [dict(account_id='test',symbol='F',order_id='1',strategy='grid')]
        claims = [] if not inventory else [dict(id=1,symbol='F',strategy='grid',claim_state='ACTIVE',**({'expires_at':(parse_utc(at)-timedelta(seconds=1)).isoformat()} if margin_scope else {}))]
        paths = certify_owned_inventory_paths('test',{p['symbol']:D(p['positionAmt']) for p in inventory},[dict(f,trade_time_utc=parse_utc(at)) for f in fills],owners,coverage_complete=True)
        parent = dict(account_id='test',as_of=at,available_at=at,capture=dict(inventory=inventory,independent=inventory,local={}),fills=fills,order_owners=owners,inventory_owner_path_proofs=paths,source_collection=dict(complete=True,gaps=[],position_symbols_covered=[p['symbol'] for p in inventory]),income_report=dict(id_match_and_coverage_complete=True,events=[],coverage_gaps=[]),allocation_audit=dict(version=1,journal=[]),result=dict(ready=True))
        finance = row['payload']['financial_context']
        finance['allocation'] = allocation
        finance['entry_policy'].update(account_id='test',reason=json.dumps({'hrs_risk_budget_version':version}) if version else '',entry_ceiling=ceiling)
        if margin_scope:
            finance['entry_policy']['reason']=json.dumps({'hrs_risk_budget_version':version,'hrs_margin_scope':margin_scope})
            finance.update(hrs_margin_scope=margin_scope,account_available_balance=cash,account_available_balance_source='papi/v1/account.totalAvailableBalance',account_status='NORMAL',account_cash_observed_at=at,account_pending_reservations=[])
        finance.update(runtime_slot_scope=SLOT_SCOPE,exchange_positions=inventory,independent_positions=inventory,claims=claims,ownership_claims=claims,hrs_slot_inputs=json.loads(canonical(capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local={},observed_at=parse_utc(at)))[0]),equity_gate=gate(dict(payload=parent,sha256=canonical(parent)[1]),parse_utc(at)))
        sources['equity_evidence'].append(dict(payload=parent,evidence_sha256=canonical(parent)[1]))
        row['payload_sha256'] = canonical_payload(row['payload'])[1]
        for stage in sources['decision_evidence']:
            if stage['run_id']==row['run_id'] and stage['kind']=='entry_stage_inputs':
                stage['payload'].update(claims=claims,exchange_positions=inventory)
                stage['payload_sha256']=canonical_payload(stage['payload'])[1]
    if source_transform:source_transform(sources)
    result = export_evidence_bundle(sources,tmp_path/'out',window_start=AT,as_of=AT+timedelta(hours=3),execution_model=bootstrap['execution_model'],monitoring_model=bootstrap['monitoring_model'],allow_inception_segment=True)
    if export_result:return result
    assert result.get('status')=='SEALED', result
    portal = DataPortal(result['snapshot_dir'],'hrs',AT+timedelta(hours=3),AT)
    states = json.loads((Path(result['snapshot_dir'])/'hrs_state_tables.json').read_text())
    return portal, HrsAdapter(lambda at:states)


def test_legacy_tiny_budget_stays_rejected_but_explicit_floor_opens_minimum(tmp_path):
    old, old_adapter = floor_fixture(tmp_path/'old', version=None)
    legacy = run_replay(old, old_adapter, broker())
    assert legacy.final_position is None
    portal, adapter = floor_fixture(tmp_path/'floor')
    result = run_replay(portal, adapter, broker())
    assert result.final_position is not None
    budget = next(e['payload'] for e in result.audit_events if e['type']=='hrs_budget' and e['payload']['allowed'])
    assert budget['budget_version'] == FLOOR
    assert D(budget['quantity']) == D(budget['minimum_quantity'])
    assert D(budget['worst_loss']) > D(budget['base_single_limit'])
    assert D(budget['worst_loss']) <= D(budget['risk_limit'])


def test_floor_lock_refines_down_for_full_account_margin(tmp_path):
    portal, adapter = floor_fixture(tmp_path, allocation='100', foreign_margin='1')
    result = run_replay(portal, adapter, broker())
    assert result.final_position is not None
    assert result.final_position.entry_quantity == D('.06')
    reservation = next(e['payload'] for e in result.audit_events if e['type']=='hrs_account_reservation')
    assert D(reservation['requested_margin']) <= D('3')


def test_floor_physical_margin_still_blocks_minimum(tmp_path):
    portal, adapter = floor_fixture(tmp_path, foreign_margin='2')
    assert run_replay(portal, adapter, broker()).final_position is None


def test_unknown_captured_budget_version_is_data_blocked(tmp_path):
    portal, adapter, _ = _hrs_fixture(tmp_path)
    state = json.loads((portal.snapshot_dir/'hrs_replay_state.json').read_text())
    state['entry_policy']['reason'] = json.dumps({'hrs_risk_budget_version':'future_floor'})
    portal = refreeze(portal, {'hrs_replay_state.json':json.dumps(state)})
    with pytest.raises(DataBlocked,match='budget_version'):
        run_replay(portal, adapter, broker())


@pytest.mark.parametrize('pressure',['5',None])
def test_floor_locked_pending_inventory_requires_and_retains_pressure_risk(tmp_path,pressure):
    from datetime import timedelta
    from .test_hrs_wiring import AT
    from strategies.hrs.risk_budget import assess_entry
    portal, adapter = floor_fixture(tmp_path, allocation='100')
    ctx=adapter.prepare(portal)
    ctx.portfolio=broker().view()
    adapter._bootstrap(ctx)
    now=AT+timedelta(hours=1)
    raw=portal.hrs_inputs('A',now,decision_at=now)
    decision=assess_entry(budget_version=FLOOR,allocation=D(100),entry_price=D(100),stop_price=D(95),direction='long',existing_open_risk=D(0),leverage=D(2),remaining_margin=D(4),qty_step=D('.001'),min_qty=D('.001'),min_notional=D(5),fee_rate=D('.0005'),pressure_slippage=D('.02'),take_profit_prices=(D(103),D(107)))
    pending=dict(margin=D('.5'),terminal=False)
    if pressure is not None:pending['risk']=D(pressure)
    adapter._durable_margin_reservations['P']=pending
    refined=[]
    if pressure is None:
        with pytest.raises(DataBlocked,match='retained_pressure_risk'):
            adapter._reservation_denial('A',decision.quantity,D(100),ctx,now,raw['risk'],D(4),decision=decision,refined_decisions=refined)
    else:
        assert adapter._reservation_denial('A',decision.quantity,D(100),ctx,now,raw['risk'],D(4),decision=decision,refined_decisions=refined) is None
        assert refined[0].budget_inputs['existing_open_risk']==D(pressure)
        assert refined[0].quantity==decision.minimum_quantity<decision.quantity
        assert refined[0].budget_inputs['remaining_margin']<=decision.quantity*D(50)
