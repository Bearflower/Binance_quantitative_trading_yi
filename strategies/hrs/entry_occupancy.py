"""Positive HRS slot ownership; account margin remains a separate full inventory.

No exchange/DB calls occur here. Callers retain two actual GET inventories and
local HRS state. The risk-policy lock supplies the current authorized raw equity
parent and current active claims. Missing local state never identifies a foreign
owner: every nonzero account position requires its complete signed owner path.
"""
import copy
import json
import re
from datetime import datetime,timedelta,timezone
from decimal import Decimal

from .equity_evidence import (canonical,_inventory,_inventory_agrees,
    certify_owned_inventory_paths,owned_fill_positions)

SLOT_SCOPE='hrs_owned_and_unreleased_reservations_v2'
INPUT_VERSION='hrs-slot-inputs-v1'
PROOF_VERSION='hrs-slot-ownership-v1'
_HASH=re.compile(r'[0-9a-f]{64}\Z')


class HrsOccupancyDenied(RuntimeError):
    def __init__(self,reason):
        self.reason_code=reason
        super().__init__(reason)


def _require(condition,reason):
    if not condition:raise HrsOccupancyDenied(reason)


def _time(value):
    try:
        if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
        _require(isinstance(value,datetime) and value.tzinfo is not None and value.utcoffset() is not None,'hrs_slot_time_invalid')
        return value.astimezone(timezone.utc)
    except (ValueError,TypeError):raise HrsOccupancyDenied('hrs_slot_time_invalid') from None


def capture_hrs_slot_inputs(*,inventory,independent,local,observed_at):
    _require(isinstance(inventory,list) and isinstance(independent,list) and isinstance(local,dict),'hrs_slot_inventory_missing')
    return copy.deepcopy(dict(version=INPUT_VERSION,observed_at=_time(observed_at),inventory=inventory,independent=independent,local=local))


def _local(local):
    _require(isinstance(local,dict),'hrs_slot_local_missing')
    result={}
    for symbol,row in local.items():
        _require(isinstance(symbol,str) and symbol and isinstance(row,dict),'hrs_slot_local_invalid')
        qty=Decimal(str(row.get('remaining_quantity')))
        _require(qty.is_finite() and qty>0 and row.get('direction') in ('long','short') and bool(row.get('position_id')),'hrs_slot_local_invalid')
        result[symbol]=(qty if row['direction']=='long' else -qty,row['position_id'])
    return result


def _account_margin(inventory):
    result=Decimal(0)
    for row in inventory:
        qty=Decimal(str(row['positionAmt']))
        _require(qty.is_finite(),'hrs_slot_account_margin_invalid')
        if qty==0:continue
        mark,leverage=Decimal(str(row['markPrice'])),Decimal(str(row['leverage']))
        _require(mark.is_finite() and mark>0 and leverage.is_finite() and leverage>0,'hrs_slot_account_margin_invalid')
        result+=abs(qty)*mark/leverage
    return result


def prove_hrs_slot_ownership(account_id,parent,inputs,claims,*,checked_at):
    """Return a minimal audit proof, or deny every unknown/conflicting owner."""
    try:
        payload=json.loads(parent['payload']) if isinstance(parent['payload'],str) else parent['payload']
        _require(canonical(payload)[1]==parent['evidence_sha256'],'hrs_slot_parent_hash_mismatch')
        _require(parent.get('account_id',account_id)==payload.get('account_id')==account_id,'hrs_slot_account_mismatch')
        _require(isinstance(inputs,dict) and inputs.get('version')==INPUT_VERSION,'hrs_slot_input_version_invalid')
        now,observed,as_of=_time(checked_at),_time(inputs['observed_at']),_time(payload['as_of'])
        _require(as_of<=observed<=now and now-as_of<=timedelta(seconds=120),'hrs_slot_source_stale_or_future')
        _require(_time(payload['available_at'])<=now,'hrs_slot_parent_unavailable')
        collection=payload['source_collection']
        _require(collection.get('complete') is True and collection.get('gaps')==[],'hrs_slot_coverage_incomplete')
        capture=payload['capture']
        actual=_inventory(inputs['inventory'])
        _require(_inventory_agrees(actual,_inventory(inputs['independent'],basis_required=False)),'hrs_slot_inventory_conflict')
        _require(actual==_inventory(capture['inventory']) and _inventory_agrees(actual,_inventory(capture['independent'],basis_required=False)),'hrs_slot_inventory_changed')
        _require(set(actual)<=set(collection.get('position_symbols_covered',())),'hrs_slot_coverage_incomplete')
        fills=[dict(row,trade_time_utc=_time(row['trade_time_utc'])) for row in payload['fills']]
        _require(all(row['trade_time_utc']<=as_of for row in fills),'hrs_slot_future_fill')
        owners=payload['order_owners']
        paths=certify_owned_inventory_paths(account_id,{symbol:value[0] for symbol,value in actual.items()},fills,owners,coverage_complete=True)
        _require(canonical(paths)[1]==canonical(payload.get('inventory_owner_path_proofs'))[1],'hrs_slot_path_proof_mismatch')
        positions=owned_fill_positions(account_id,fills,owners,through=as_of)
        assigned={}
        for symbol,(qty,_) in actual.items():
            matches=[owner for (owner,sym),(quantity,_) in positions.items() if sym==symbol and quantity!=0 and quantity==qty]
            _require(len(matches)==1 and isinstance(matches[0],str) and bool(matches[0]),'hrs_slot_owner_unknown')
            choices={row.get('strategy') for row in claims if row.get('symbol')==symbol and row.get('id') is not None and row.get('claim_state') in ('PENDING','ACTIVE')}
            _require(choices=={matches[0]},'hrs_slot_claim_owner_conflict')
            assigned[symbol]=matches[0]
        hrs={symbol for symbol,owner in assigned.items() if owner=='hrs'}
        local=_local(inputs['local'])
        _require(local==_local(capture['local']) and set(local)==hrs,'hrs_slot_local_owner_conflict')
        _require(all(local[symbol][0]==actual[symbol][0] for symbol in hrs),'hrs_slot_local_quantity_conflict')
        return dict(version=PROOF_VERSION,slot_scope=SLOT_SCOPE,account_id=account_id,
            equity_evidence_sha256=parent['evidence_sha256'],as_of=as_of.isoformat(),observed_at=observed.isoformat(),
            account_symbols=sorted(actual),hrs_symbols=sorted(hrs),owners=assigned,
            inventory_sha256=canonical({symbol:[str(v) for v in values] for symbol,values in sorted(actual.items())})[1],
            account_margin=str(_account_margin(inputs['inventory'])))
    except HrsOccupancyDenied:raise
    except Exception as exc:
        raise HrsOccupancyDenied('hrs_slot_ownership_unproven') from exc


def validate_hrs_slot_proof(proof,*,inventory,account_id,observed_at):
    """Validate an already source/manifest-bound proof, not recertify ownership.

    Offline exporters must first call prove_hrs_slot_ownership against the raw
    parent and claims. This smaller validator binds that result to its captured
    inventory, clocks and positive complete owner partition for replay use.
    """
    try:
        _require(isinstance(proof,dict) and set(proof)=={'version','slot_scope','account_id','equity_evidence_sha256','as_of','observed_at','account_symbols','hrs_symbols','owners','inventory_sha256','account_margin'},'hrs_slot_proof_invalid')
        _require(proof['version']==PROOF_VERSION and proof['slot_scope']==SLOT_SCOPE and proof['account_id']==account_id,'hrs_slot_proof_scope_invalid')
        _require(isinstance(proof['equity_evidence_sha256'],str) and _HASH.fullmatch(proof['equity_evidence_sha256']),'hrs_slot_proof_hash_invalid')
        observed,as_of=_time(observed_at),_time(proof['as_of'])
        _require(proof['observed_at']==observed.isoformat() and as_of<=observed<=as_of+timedelta(seconds=120),'hrs_slot_source_stale_or_future')
        actual=_inventory(inventory)
        _require(proof['account_symbols']==sorted(actual) and isinstance(proof['owners'],dict) and set(proof['owners'])==set(actual),'hrs_slot_proof_partition_invalid')
        _require(all(isinstance(owner,str) and owner for owner in proof['owners'].values()),'hrs_slot_owner_unknown')
        _require(proof['hrs_symbols']==sorted(symbol for symbol,owner in proof['owners'].items() if owner=='hrs'),'hrs_slot_proof_partition_invalid')
        digest=canonical({symbol:[str(v) for v in values] for symbol,values in sorted(actual.items())})[1]
        _require(proof['inventory_sha256']==digest and Decimal(proof['account_margin'])==_account_margin(inventory),'hrs_slot_proof_inventory_mismatch')
        return copy.deepcopy(proof)
    except HrsOccupancyDenied:raise
    except Exception as exc:raise HrsOccupancyDenied('hrs_slot_proof_invalid') from exc
