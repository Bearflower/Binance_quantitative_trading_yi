"""Engineering provenance only: synthetic facts cannot qualify financial performance."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json

import pytest

from shared.entry_risk_policy import ScopedRiskSnapshot, SizedEntry, _hrs_budget_json, _refine_hrs_floor
from strategies.hrs.acceptance_report import _engineering_checks
from strategies.hrs.entry_occupancy import SLOT_SCOPE, capture_hrs_slot_inputs, prove_hrs_slot_ownership
from strategies.hrs.equity_evidence import canonical, certify_owned_inventory_paths
from strategies.hrs.risk_budget import FLOOR, assess_entry, calculate_open_position_risk
from tests.test_strategies.test_hrs_acceptance_report import ACCOUNT, START, NOW, event


def floor_evidence():
    at = START-timedelta(seconds=3)
    local = {'L':dict(position_id='local',direction='short',remaining_quantity='1',entry_price='.24',risk_stop_price='.25',algo_ids={'sl':'sl-local'})}
    inventory = [dict(symbol='L',positionAmt='-1',entryPrice='.24',markPrice='.24',leverage='2')]
    fills = [dict(account_id=ACCOUNT,symbol='L',trade_id=900,order_id=900,side='SELL',qty='1',price='.24',trade_time_utc=at)]
    owners = [dict(account_id=ACCOUNT,symbol='L',order_id='900',strategy='hrs')]
    claims = [dict(id=900,symbol='L',strategy='hrs',claim_state='ACTIVE')]
    paths = certify_owned_inventory_paths(ACCOUNT,{'L':D(-1)},fills,owners,coverage_complete=True)
    parent = _hrs_budget_json(dict(account_id=ACCOUNT,as_of=at,available_at=at,capture=dict(inventory=inventory,independent=inventory,local=local),fills=fills,order_owners=owners,inventory_owner_path_proofs=paths,source_collection=dict(complete=True,gaps=[],position_symbols_covered=['L'])))
    digest = canonical(parent)[1]
    proof = prove_hrs_slot_ownership(ACCOUNT,dict(payload=parent,evidence_sha256=digest),capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local=local,observed_at=at),claims,checked_at=at)
    policy = dict(account_id=ACCOUNT,strategy_id='hrs',revision=2,mode='reduced',allow_add=False,multiplier='1',ratio_cap='1',entry_ceiling='5.6',effective_at=(at-timedelta(hours=1)).isoformat(),expires_at=(at+timedelta(hours=1)).isoformat(),reason=json.dumps({'hrs_risk_budget_version':FLOOR}))
    slot = dict(slot_scope=SLOT_SCOPE,account_id=ACCOUNT,policy_revision=2,strategy_id='hrs',checked_at=at.isoformat(),as_of=at.isoformat(),observed_at=at.isoformat(),evidence_sha256=digest,ownership_claims=claims,account_margin='.12',account_symbols=['L'],hrs_symbols=['L'],reservation_symbols=[],occupied_symbols=['L'],observed_slots=1,requested_slots=2,max_slots=3)
    initial = assess_entry(budget_version=FLOOR,allocation=D('5.6'),entry_price=D('.24'),stop_price=D('.25'),direction='short',existing_open_risk=calculate_open_position_risk(local,fee_rate=D('.0005'),pressure_slippage=D('.02')),leverage=D(2),remaining_margin=D('5.48'),qty_step=D(1),min_qty=D(1),min_notional=D(5),fee_rate=D('.0005'),pressure_slippage=D('.02'),take_profit_prices=(D('.225'),D('.22')))
    initial = replace(initial,policy_revision=2,effective_allocation=D('5.6'))
    assert initial.allowed and initial.quantity==D(23)
    size = SizedEntry(initial.quantity,initial.quantity,D('.24'),D(2),D('2.76'),initial.worst_loss,initial.worst_loss,initial.exit_ratios)
    geometry = dict(hrs_budget_source='hrs_live_assessor_v1',hrs_risk_budget_version=FLOOR,hrs_budget_inputs=_hrs_budget_json(initial.budget_inputs),hrs_budget_decision=_hrs_budget_json(initial))
    _, committed, context = _refine_hrs_floor(size,geometry,ScopedRiskSnapshot(D('.12'),D(100),D('5.6'),3,SLOT_SCOPE,dict(local=local)),[],D('5.6'),D('5.48'),dict(slot,locked_policy=policy,snapshot_effective_allocation='5.6',snapshot_account_equity='100',margin_cap='5.6',reserved_margin='0'))
    decision = _hrs_budget_json(replace(committed,client_order_id='risk_floor'))
    records = [event('r','scan_start',at,dict(financial_context=dict(entry_policy=policy))),
        event('r','entry_stage_inputs',at,dict(stage='reservation_success',client_order_id='risk_floor',budget_decision=geometry['hrs_budget_decision'],budget_context=context,slot_context=slot,slot_occupancy_proof=proof),'S'),
        event('r','entry_budget_reserved',at+timedelta(seconds=1),dict(decision=decision,entry_price='.24',stop_price='.25',take_profit_prices=['.225','.22'],account_equity='100',entry_mode='standard'),'S'),
        event('r','order_execution_result',START,dict(outcome=dict(order=dict(symbol='S',orderId=1,clientOrderId='risk_floor'))),'S')]
    lifecycle = dict(symbol='S',order_ids=['1','2'],opened_at=START.isoformat(),closed_at=(START+timedelta(minutes=5)).isoformat(),fill_legs=[dict(side='SELL',quantity='23',price='.24',at=START.isoformat(),order_id='1',trade_id='1'),dict(side='BUY',quantity='23',price='.225',at=(START+timedelta(minutes=5)).isoformat(),order_id='2',trade_id='2')])
    return lifecycle, records, [dict(payload=parent,evidence_sha256=digest)]


def checks(lifecycle, records, archives):
    return _engineering_checks(lifecycle,records,NOW,ACCOUNT,budget_source_archives=archives)


def test_floor_above_one_percent_reconciles_only_with_original_raw_parent():
    life, rows, archives = floor_evidence()
    budget = checks(life,rows,archives)['budget']
    assert budget['status']=='RECONCILED'
    assert D(budget['worst_loss'])>D(budget['risk_allocation'])*D('.01')
    assert checks(life,rows,[])['budget']['status']=='PENDING'


@pytest.mark.parametrize('problem',['quantity','unknown_version','input','policy','parent_hash','parent_risk','missing_stage','pending_source'])
def test_floor_changed_financial_provenance_remains_pending(problem):
    life, rows, archives = floor_evidence()
    budget = next(r for r in rows if r['kind']=='entry_budget_reserved')['payload']['decision']
    stage = next(r for r in rows if r['kind']=='entry_stage_inputs')['payload']
    if problem=='quantity':budget['quantity']='24'
    elif problem=='unknown_version':budget['budget_version']='future'
    elif problem=='input':budget['budget_inputs']['allocation']='100'
    elif problem=='policy':stage['budget_context']['locked_policy']['reason']='old policy'
    elif problem=='parent_hash':archives[0]['evidence_sha256']='0'*64
    elif problem=='parent_risk':archives[0]['payload']['capture']['local']['L']['risk_stop_price']='.24001'
    elif problem=='pending_source':stage['budget_context']['pending_reservations']=[dict(symbol='P',pressure_risk='0')]
    else:rows=[r for r in rows if r['kind']!='entry_stage_inputs']
    from strategies.hrs.decision_capture import canonical_payload
    for row in rows:row['payload_sha256']=canonical_payload(row['payload'])[1]
    assert checks(life,rows,archives)['budget']['status']=='PENDING'


@pytest.mark.parametrize('problem',['owner_claim','source_local_rehashed','missing_mode','wrong_run'])
def test_floor_original_owner_and_declared_mode_must_match(problem):
    life, rows, archives = floor_evidence()
    stage = next(r for r in rows if r['kind']=='entry_stage_inputs')
    if problem=='owner_claim':stage['payload']['budget_context']['ownership_claims'][0]['strategy']='grid'
    elif problem=='source_local_rehashed':
        archives[0]['payload']['capture']['local']['L']['risk_stop_price']='.24001'
        digest=canonical(archives[0]['payload'])[1]
        archives[0]['evidence_sha256']=digest
        for target in (stage['payload']['budget_context'],stage['payload']['slot_context']):target['evidence_sha256']=digest
        stage['payload']['slot_occupancy_proof']['equity_evidence_sha256']=digest
    elif problem=='missing_mode':
        next(r for r in rows if r['kind']=='entry_budget_reserved')['payload']['decision'].pop('budget_version')
        next(r for r in rows if r['kind']=='entry_budget_reserved')['payload']['decision'].update(risk_limit='.01',worst_loss='.01')
    else:
        stage['run_id']='other'
        rows.append(event('other','scan_start',START-timedelta(seconds=4),dict(financial_context=dict(entry_policy=dict(account_id=ACCOUNT)))))
    from strategies.hrs.decision_capture import canonical_payload
    for row in rows:row['payload_sha256']=canonical_payload(row['payload'])[1]
    assert checks(life,rows,archives)['budget']['status']=='PENDING'


@pytest.mark.asyncio
async def test_reader_fetches_raw_parents_only_for_first_five_samples():
    from strategies.hrs.acceptance_report import _read_budget_sources
    life, rows, archives = floor_evidence()
    class Connection:
        async def fetch(self, sql, account, digests, through):
            self.digests=digests
            assert account==ACCOUNT and len(digests)<=5
            return archives
    connection=Connection()
    result=await _read_budget_sources(connection,dict(samples=[life]*5),rows,ACCOUNT,NOW)
    assert result==archives
    assert connection.digests==[archives[0]['evidence_sha256']]


@pytest.mark.parametrize(('field','value'), [('snapshot_effective_allocation','2.5'),('snapshot_account_equity','2.5'),('margin_cap','100'),('reserved_margin','1'),('remaining_margin','5.47')])
def test_floor_exact_locked_financial_sources_are_reproduced(field,value):
    life,rows,archives=floor_evidence()
    stage=next(r for r in rows if r['kind']=='entry_stage_inputs')
    stage['payload']['budget_context'][field]=value
    from strategies.hrs.decision_capture import canonical_payload
    stage['payload_sha256']=canonical_payload(stage['payload'])[1]
    assert checks(life,rows,archives)['budget']['status']=='PENDING'
