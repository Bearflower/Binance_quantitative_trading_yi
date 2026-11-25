"""Engineering provenance only: synthetic facts cannot qualify financial performance."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json,hashlib

import pytest

from shared.entry_risk_policy import ScopedRiskSnapshot, SizedEntry, _hrs_budget_json, _refine_hrs_floor
from strategies.hrs.acceptance_report import _engineering_checks
from strategies.hrs.entry_occupancy import SLOT_SCOPE, capture_hrs_slot_inputs, prove_hrs_slot_ownership
from strategies.hrs.equity_evidence import canonical, certify_owned_inventory_paths
from strategies.hrs.risk_budget import FLOOR, assess_entry, calculate_open_position_risk
from tests.test_strategies.test_hrs_acceptance_report import ACCOUNT, START, NOW, event


CLIENT='risk_'+hashlib.sha256(f'{ACCOUNT}:hrs:signal'.encode()).hexdigest()[:30]


def floor_evidence(*, current_mark='.24', margin_scope=None):
    at = START-timedelta(seconds=3)
    local = {'L':dict(position_id='local',direction='short',remaining_quantity='1',entry_price='.24',risk_stop_price='.25',algo_ids={'sl':'sl-local'})}
    inventory = [dict(symbol='L',positionAmt='-1',entryPrice='.24',markPrice='.24',leverage='2')]
    fills = [dict(account_id=ACCOUNT,symbol='L',trade_id=900,order_id=900,side='SELL',qty='1',price='.24',trade_time_utc=at)]
    owners = [dict(account_id=ACCOUNT,symbol='L',order_id='900',strategy='hrs')]
    claims = [dict(id=900,symbol='L',strategy='hrs',claim_state='ACTIVE')]
    if margin_scope:
        inventory.append(dict(symbol='F',positionAmt='1',entryPrice='129.72',markPrice='129.72',leverage='2'))
        fills.append(dict(account_id=ACCOUNT,symbol='F',trade_id=901,order_id=901,side='BUY',qty='1',price='129.72',trade_time_utc=at))
        owners.append(dict(account_id=ACCOUNT,symbol='F',order_id='901',strategy='grid'))
        claims.append(dict(id=901,symbol='F',strategy='grid',claim_state='ACTIVE',expires_at=(at-timedelta(seconds=1)).isoformat()))
    paths = certify_owned_inventory_paths(ACCOUNT,{r['symbol']:D(r['positionAmt']) for r in inventory},fills,owners,coverage_complete=True)
    parent = _hrs_budget_json(dict(account_id=ACCOUNT,as_of=at,available_at=at,capture=dict(inventory=inventory,independent=inventory,local=local),fills=fills,order_owners=owners,inventory_owner_path_proofs=paths,source_collection=dict(complete=True,gaps=[],position_symbols_covered=[r['symbol'] for r in inventory])))
    digest = canonical(parent)[1]
    current_inventory = [dict(row,markPrice=current_mark if row['symbol']=='L' else row['markPrice']) for row in inventory]
    slot_inputs = _hrs_budget_json(capture_hrs_slot_inputs(inventory=current_inventory,independent=current_inventory,local=local,observed_at=at))
    proof = prove_hrs_slot_ownership(ACCOUNT,dict(payload=parent,evidence_sha256=digest),slot_inputs,claims,checked_at=at,**({'margin_scope':margin_scope} if margin_scope else {}))
    margin = D(current_mark)/2+(D('64.86') if margin_scope else 0)
    financial_row = event('r','entry_stage_inputs',at,dict(stage='reservation_inventory',account_id=ACCOUNT,signal_id='signal',exchange_positions=current_inventory,slot_scope=SLOT_SCOPE,slot_inputs=slot_inputs,account_equity='100',effective_allocation='5.6',observed_at=at.isoformat()),'S')
    if margin_scope:
        financial_row['payload'].update(hrs_margin_scope=margin_scope,account_available_balance='448.86',account_available_balance_source='papi/v1/account.totalAvailableBalance',account_status='NORMAL',account_cash_observed_at=at.isoformat())
        from strategies.hrs.decision_capture import canonical_payload
        financial_row['payload_sha256']=canonical_payload(financial_row['payload'])[1]
    financial_row['recorded_at'] = at
    receipt = {key: financial_row[key] for key in ('evidence_id','run_id','payload_sha256')}
    receipt.update(event_time=at.isoformat(),available_at=at.isoformat())
    policy = dict(account_id=ACCOUNT,strategy_id='hrs',revision=2,mode='reduced',allow_add=False,multiplier='1',ratio_cap='1',entry_ceiling='5.6',effective_at=(at-timedelta(hours=1)).isoformat(),expires_at=(at+timedelta(hours=1)).isoformat(),reason=json.dumps({'hrs_risk_budget_version':FLOOR}))
    slot = dict(slot_scope=SLOT_SCOPE,account_id=ACCOUNT,policy_revision=2,strategy_id='hrs',checked_at=at.isoformat(),as_of=at.isoformat(),observed_at=at.isoformat(),evidence_sha256=digest,ownership_claims=claims,account_margin=str(margin),account_symbols=sorted(r['symbol'] for r in inventory),hrs_symbols=['L'],reservation_symbols=[],occupied_symbols=['L'],observed_slots=1,requested_slots=2,max_slots=3)
    if margin_scope:
        from strategies.hrs.margin_budget import margin_limits
        policy['reason']=json.dumps({'hrs_risk_budget_version':FLOOR,'hrs_margin_scope':margin_scope})
        limits=margin_limits(financial_row['payload'],proof,[],[],D('5.6'),policy)
        slot.update(_hrs_budget_json(limits),hrs_margin_scope=margin_scope,account_pending_reservations=[],
            **{k:financial_row['payload'][k] for k in ('account_available_balance_source','account_status','account_cash_observed_at')})
    initial = assess_entry(budget_version=FLOOR,allocation=D('5.6'),entry_price=D('.24'),stop_price=D('.25'),direction='short',existing_open_risk=calculate_open_position_risk(local,fee_rate=D('.0005'),pressure_slippage=D('.02')),leverage=D(2),remaining_margin=D('5.48'),qty_step=D(1),min_qty=D(1),min_notional=D(5),fee_rate=D('.0005'),pressure_slippage=D('.02'),take_profit_prices=(D('.225'),D('.22')))
    initial = replace(initial,policy_revision=2,effective_allocation=D('5.6'))
    assert initial.allowed and initial.quantity==D(23)
    size = SizedEntry(initial.quantity,initial.quantity,D('.24'),D(2),D('2.76'),initial.worst_loss,initial.worst_loss,initial.exit_ratios)
    geometry = dict(hrs_budget_source='hrs_live_assessor_v1',hrs_risk_budget_version=FLOOR,hrs_budget_inputs=_hrs_budget_json(initial.budget_inputs),hrs_budget_decision=_hrs_budget_json(initial))
    _, committed, context = _refine_hrs_floor(size,geometry,ScopedRiskSnapshot(margin,D(100),D('5.6'),3,SLOT_SCOPE,dict(local=local)),[],D('5.6'),D('5.6')-D(current_mark)/2,dict(slot,locked_policy=policy,snapshot_effective_allocation='5.6',snapshot_account_equity='100',margin_cap='5.6',reserved_margin='0',financial_receipt=receipt,symbol='S',signal_id='signal'))
    decision = _hrs_budget_json(replace(committed,client_order_id=CLIENT))
    records = [event('r','scan_start',at,dict(financial_context=dict(entry_policy=policy))),financial_row,
        event('r','entry_stage_inputs',at,dict(stage='reservation_success',client_order_id=CLIENT,budget_decision=geometry['hrs_budget_decision'],budget_context=context,slot_context=slot,slot_occupancy_proof=proof),'S'),
        event('r','entry_budget_reserved',at+timedelta(seconds=1),dict(decision=decision,entry_price='.24',stop_price='.25',take_profit_prices=['.225','.22'],account_equity='100',entry_mode='standard'),'S'),
        event('r','order_execution_result',START,dict(outcome=dict(order=dict(symbol='S',orderId=1,clientOrderId=CLIENT))),'S')]
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
    stage = next(r for r in rows if r['kind']=='entry_stage_inputs' and r['payload'].get('stage')=='reservation_success')['payload']
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
    stage = next(r for r in rows if r['kind']=='entry_stage_inputs' and r['payload'].get('stage')=='reservation_success')
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
        async def cursor(self, sql, account, digests, through, *, prefetch):
            self.digests=digests
            assert account==ACCOUNT and len(digests)<=5
            assert prefetch==1 and 'CASE WHEN' in sql
            for archive in archives:
                yield archive
    connection=Connection()
    result=await _read_budget_sources(connection,dict(samples=[life]*5),rows,ACCOUNT,NOW)
    assert result==archives
    assert connection.digests==[archives[0]['evidence_sha256']]


@pytest.mark.parametrize(('field','value'), [('snapshot_effective_allocation','2.5'),('snapshot_account_equity','2.5'),('margin_cap','100'),('reserved_margin','1'),('remaining_margin','5.47')])
def test_floor_exact_locked_financial_sources_are_reproduced(field,value):
    life,rows,archives=floor_evidence()
    stage=next(r for r in rows if r['kind']=='entry_stage_inputs' and r['payload'].get('stage')=='reservation_success')
    stage['payload']['budget_context'][field]=value
    from strategies.hrs.decision_capture import canonical_payload
    stage['payload_sha256']=canonical_payload(stage['payload'])[1]
    assert checks(life,rows,archives)['budget']['status']=='PENDING'


def test_floor_without_independent_reservation_inventory_receipt_remains_pending():
    life,rows,archives=floor_evidence()
    rows=[r for r in rows if not (r['kind']=='entry_stage_inputs' and r['payload'].get('stage')=='reservation_inventory')]
    assert checks(life,rows,archives)['budget']['status']=='PENDING'


def test_floor_current_mark_change_uses_independent_inventory_receipt_for_margin():
    life,rows,archives=floor_evidence(current_mark='.245')
    assert checks(life,rows,archives)['budget']['status']=='RECONCILED'


@pytest.mark.parametrize('field,value',[('account_equity','1000'),('effective_allocation','1000'),('signal_id','other')])
def test_floor_finance_receipt_payload_cannot_be_replaced(field,value):
    life,rows,archives=floor_evidence()
    source=next(r for r in rows if r['kind']=='entry_stage_inputs' and r['payload'].get('stage')=='reservation_inventory')
    source['payload'][field]=value
    assert checks(life,rows,archives)['budget']['status']=='PENDING'


def test_floor_receipt_protection_fields_must_match_immutable_equity_parent():
    from strategies.hrs.decision_capture import canonical_payload
    life,rows,archives=floor_evidence()
    source=next(r for r in rows if r['kind']=='entry_stage_inputs' and r['payload'].get('stage')=='reservation_inventory')
    stage=next(r for r in rows if r['kind']=='entry_stage_inputs' and r['payload'].get('stage')=='reservation_success')
    source['payload']['slot_inputs']['local']['L']['risk_stop_price']='.5'
    source['payload_sha256']=canonical_payload(source['payload'])[1]
    stage['payload']['budget_context']['financial_receipt']['payload_sha256']=source['payload_sha256']
    stage['payload_sha256']=canonical_payload(stage['payload'])[1]
    assert checks(life,rows,archives)['budget']['status']=='PENDING'


def test_owned_floor_acceptance_reproduces_actual_cash_and_scope():
    from strategies.hrs.margin_budget import HRS_OWNED
    life,rows,archives=floor_evidence(margin_scope=HRS_OWNED)
    assert checks(life,rows,archives)['budget']['status']=='RECONCILED'


@pytest.mark.parametrize('target,field,value',[
    ('receipt','account_available_balance','448.87'),('receipt','account_available_balance_source','inferred'),
    ('receipt','account_status','MARGIN_CALL'),('receipt','account_cash_observed_at',None),
    ('receipt','hrs_margin_scope',None),('context','hrs_margin_scope',None),
    ('context','account_available_balance','448.87'),('context','account_pending_reservations',None),
    ('slot','account_pending_reservations',[dict(state='reserved')]),
    ('proof','claim_scope','old_claims'),
])
def test_owned_acceptance_cash_scope_pending_and_proof_tampering_stays_pending(target,field,value):
    from strategies.hrs.margin_budget import HRS_OWNED
    from strategies.hrs.decision_capture import canonical_payload
    life,rows,archives=floor_evidence(margin_scope=HRS_OWNED)
    source=next(r for r in rows if r['payload'].get('stage')=='reservation_inventory')
    stage=next(r for r in rows if r['payload'].get('stage')=='reservation_success')
    dst={'receipt':source['payload'],'context':stage['payload']['budget_context'],
        'slot':stage['payload']['slot_context'],'proof':stage['payload']['slot_occupancy_proof']}[target]
    dst[field]=value
    # Preserve envelope validity for context tampering; independent receipt hashes
    # remain the authority for the actual GET cash fields.
    stage['payload_sha256']=canonical_payload(stage['payload'])[1]
    assert checks(life,rows,archives)['budget']['status']=='PENDING'


@pytest.mark.parametrize('field,value',[('account_available_balance','1'),('account_available_balance_source','inferred'),('account_status','MARGIN_CALL'),('account_cash_observed_at',None),('hrs_margin_scope','all_account_positions_v1')])
def test_owned_acceptance_resealed_financial_receipt_still_requires_exact_contract(field,value):
    from strategies.hrs.margin_budget import HRS_OWNED
    from strategies.hrs.decision_capture import canonical_payload
    life,rows,archives=floor_evidence(margin_scope=HRS_OWNED)
    source=next(r for r in rows if r['payload'].get('stage')=='reservation_inventory')
    stage=next(r for r in rows if r['payload'].get('stage')=='reservation_success')
    source['payload'][field]=value
    source['payload_sha256']=canonical_payload(source['payload'])[1]
    stage['payload']['budget_context']['financial_receipt']['payload_sha256']=source['payload_sha256']
    stage['payload_sha256']=canonical_payload(stage['payload'])[1]
    assert checks(life,rows,archives)['budget']['status']=='PENDING'
