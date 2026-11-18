from datetime import datetime,timedelta,timezone
from decimal import Decimal
import copy,json
import pytest
from strategies.hrs.equity_evidence import canonical,certify_owned_inventory_paths

T=datetime.now(timezone.utc)


def fixture():
    inventory=[dict(symbol=s,positionAmt=q,entryPrice='10',positionSide='BOTH',markPrice='10',leverage='10')
               for s,q in (('WLDUSDT','28'),('BNBUSDT','.04'),('SOLUSDT','-.27'))]
    fills=[];owners=[];claims=[]
    for index,row in enumerate(inventory):
        owner='hrs' if index==0 else 'btc_eth_aggressive'
        fills.append(dict(account_id='test',symbol=row['symbol'],trade_id=index+1,order_id=index+1,
            side='BUY' if Decimal(row['positionAmt'])>0 else 'SELL',qty=str(abs(Decimal(row['positionAmt']))),price='10',trade_time_utc=T-timedelta(seconds=10)))
        owners.append(dict(account_id='test',symbol=row['symbol'],order_id=str(index+1),strategy=owner))
        claims.append(dict(id=index+1,symbol=row['symbol'],strategy=owner,claim_state='ACTIVE'))
    local={'WLDUSDT':dict(position_id='p',direction='long',remaining_quantity='28')}
    proof=certify_owned_inventory_paths('test',{r['symbol']:Decimal(r['positionAmt']) for r in inventory},fills,owners,coverage_complete=True)
    payload=dict(account_id='test',segment_id='seg',as_of=T,available_at=T,
        capture=dict(inventory=inventory,independent=inventory,local=local),fills=fills,order_owners=owners,
        source_collection=dict(complete=True,gaps=[],position_symbols_covered=[r['symbol'] for r in inventory]),
        inventory_owner_path_proofs=proof,result=dict(ledger_version=1))
    encoded,digest=canonical(payload)
    parent=dict(account_id='test',evidence_sha256=digest,payload=json.loads(encoded))
    return inventory,local,parent,claims


def test_hrs_only_slots_need_positive_owners_for_every_account_position():
    from strategies.hrs.entry_occupancy import capture_hrs_slot_inputs,prove_hrs_slot_ownership,SLOT_SCOPE
    inventory,local,parent,claims=fixture()
    inputs=capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local=local,observed_at=T+timedelta(seconds=1))
    proof=prove_hrs_slot_ownership('test',parent,inputs,claims,checked_at=T+timedelta(seconds=2))
    assert proof['slot_scope']==SLOT_SCOPE=='hrs_owned_and_unreleased_reservations_v2'
    assert proof['account_symbols']==['BNBUSDT','SOLUSDT','WLDUSDT']
    assert proof['hrs_symbols']==['WLDUSDT']
    assert proof['owners']=={'BNBUSDT':'btc_eth_aggressive','SOLUSDT':'btc_eth_aggressive','WLDUSDT':'hrs'}


@pytest.mark.parametrize('damage',['parent_hash','unknown_order','missing_claim','conflicting_claim','inventory','independent','local_absent','local_foreign','coverage','stale','future','path'])
def test_missing_conflicting_or_stale_ownership_never_becomes_foreign(damage):
    from strategies.hrs.entry_occupancy import capture_hrs_slot_inputs,prove_hrs_slot_ownership,HrsOccupancyDenied
    inventory,local,parent,claims=fixture()
    independent=copy.deepcopy(inventory)
    observed=T+timedelta(seconds=1)
    payload=parent['payload']
    if damage=='parent_hash':payload['unused']='tampered'
    elif damage=='unknown_order':payload['order_owners'].pop()
    elif damage=='missing_claim':claims.pop()
    elif damage=='conflicting_claim':claims.append(dict(id=999,symbol='SOLUSDT',strategy='new_coin',claim_state='ACTIVE'))
    elif damage=='inventory':inventory[0]['positionAmt']='27'
    elif damage=='independent':independent.pop()
    elif damage=='local_absent':local.clear()
    elif damage=='local_foreign':local['SOLUSDT']=dict(position_id='x',direction='short',remaining_quantity='.27')
    elif damage=='coverage':payload['source_collection']['complete']=False
    elif damage=='stale':observed=T-timedelta(seconds=121)
    elif damage=='future':observed=T+timedelta(seconds=3)
    elif damage=='path':payload['inventory_owner_path_proofs'][0]['path_sha256']='0'*64
    if damage in ('unknown_order','coverage','path'):parent['evidence_sha256']=canonical(payload)[1]
    inputs=capture_hrs_slot_inputs(inventory=inventory,independent=independent,local=local,observed_at=observed)
    with pytest.raises(HrsOccupancyDenied):prove_hrs_slot_ownership('test',parent,inputs,claims,checked_at=T+timedelta(seconds=2))


def test_manifest_bound_proof_validator_matches_inventory_clock_and_positive_owner_partition():
    from strategies.hrs.entry_occupancy import capture_hrs_slot_inputs,prove_hrs_slot_ownership,validate_hrs_slot_proof,HrsOccupancyDenied
    inventory,local,parent,claims=fixture()
    observed=T+timedelta(seconds=1)
    inputs=capture_hrs_slot_inputs(inventory=inventory,independent=inventory,local=local,observed_at=observed)
    proof=prove_hrs_slot_ownership('test',parent,inputs,claims,checked_at=T+timedelta(seconds=2))
    assert Decimal(proof['account_margin'])==Decimal('28.31')
    assert validate_hrs_slot_proof(proof,inventory=inventory,account_id='test',observed_at=observed)==proof
    for field,value in [('hrs_symbols',[]),('inventory_sha256','0'*64),('account_margin','0'),('owners',{'WLDUSDT':'hrs'}),('observed_at',T.isoformat())]:
        damaged=dict(proof,**{field:value})
        with pytest.raises(HrsOccupancyDenied):validate_hrs_slot_proof(damaged,inventory=inventory,account_id='test',observed_at=observed)
