"""Offline closure of immutable HRS funding references; never hydrate parents."""
from datetime import datetime,timezone
import json

from strategies.hrs.equity_evidence import EvidenceBlocked,canonical
from strategies.hrs.funding_reference import validate_forecasts_ref,evidence_forecasts_ref,verify_forecasts_prefixes

FAMILY='hrs_funding_observations'
INVENTORY_VERSION='hrs-funding-observation-prefix-export-v1'


def _require(condition,reason):
    if not condition:raise EvidenceBlocked('funding_forecasts_export_'+reason)


def utc(value):
    at=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    _require(isinstance(at,datetime) and at.tzinfo is not None and at.utcoffset() is not None,'time_invalid')
    return at.astimezone(timezone.utc)


def parent_reference(parent,*,account_id=None):
    """Raw hash precedes reference parsing, including during source discovery."""
    payload=json.loads(parent['payload']) if isinstance(parent['payload'],str) else parent['payload']
    _require(canonical(payload)[1]==parent['evidence_sha256'],'parent_hash_mismatch')
    capture=payload.get('capture',{})
    if 'funding_publication' not in capture:return None
    publication=capture['funding_publication']
    _require(isinstance(publication,dict),'publication_invalid')
    account=account_id or parent.get('account_id') or payload.get('account_id')
    _require(isinstance(account,str) and bool(account) and payload.get('account_id')==account
        and parent.get('account_id',account)==account,'account_mismatch')
    ref=evidence_forecasts_ref(parent,account_id=account)
    if ref is None:return None
    cutoff=validate_forecasts_ref(ref,account_id=account)
    available=parent.get('available_at',payload.get('available_at'))
    _require(available is not None and cutoff<=utc(available),'parent_availability_before_reference')
    return account,cutoff,ref


def verify_parent_closure(parents,*,source_rows,source_count,inventory):
    """Keep only distinct compact refs; source rows are streamed for each prefix.

    source_count(None) counts the independently inventoried union. One shared
    incremental pass checks every original parent prefix without repeated COUNT
    scans or re-reading/collecting earlier source rows.
    """
    refs={}
    for parent in parents:
        found=parent_reference(parent)
        if found is not None:refs[canonical(found[2])[1]]=found
    if not refs:
        _require(source_count(None)==0,'unreferenced_sources')
        return dict(reference_count=0,source_count=0)
    accounts={account for account,_cutoff,_ref in refs.values()}
    _require(len(accounts)==1,'account_mismatch')
    account=next(iter(accounts));cutoff=max(at for _account,at,_ref in refs.values())
    _require(isinstance(inventory,dict) and inventory.get('version')==INVENTORY_VERSION,'inventory_missing_or_version')
    _require(inventory.get('account_id')==account and utc(inventory['available_through'])==cutoff,'inventory_scope_mismatch')
    expected=inventory.get('expected_count')
    _require(type(expected) is int and expected>=0 and source_count(None)==expected,'inventory_count_mismatch')
    _require(source_count(cutoff)==expected,'source_after_maximum_cutoff')
    verify_forecasts_prefixes([ref for _account,_at,ref in refs.values()],source_rows(cutoff),
        account_id=account,expected_count=expected)
    return dict(reference_count=len(refs),source_count=expected,account_id=account,available_through=cutoff.isoformat())
