"""Lossless, constant-size references to append-only funding source receipts.

The prefix is every row for one account with available_at <= the actual retained
cutoff, without a time lower bound. Its order is (available_at,evidence_sha256).
Hashes cover original complete source payloads and receipt availability, not a
hydrated replacement for the signed parent. Existing inline parents stay valid.
No source query/table/algorithm is accepted from untrusted reference text.
"""
from datetime import datetime,timezone
import copy
import hashlib
import json
import re

from .equity_evidence import EvidenceBlocked,canonical

VERSION='hrs-funding-forecasts-ref-v1'
TABLE='trading.hrs_funding_observation'
ORDER='available_at,evidence_sha256'
CONTRACT='hrs-equity-canonical-v1+length-framed-source-row-v1'
_HASH=re.compile(r'[0-9a-f]{64}\Z')
_FIELDS={'version','table','account_id','available_through','row_count','forecast_count','order',
         'ordered_sha256','first_key','last_key','canonical_contract'}
COUNT_SQL='SELECT count(*) FROM trading.hrs_funding_observation WHERE account_id=$1 AND available_at<=$2'
ROWS_SQL='''SELECT account_id,evidence_sha256,available_at,payload FROM trading.hrs_funding_observation
    WHERE account_id=$1 AND available_at<=$2 ORDER BY available_at,evidence_sha256'''


def _require(condition,reason):
    if not condition:raise EvidenceBlocked('funding_forecasts_ref_'+reason)


def _time(value):
    try:
        at=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
        _require(isinstance(at,datetime) and at.tzinfo is not None and at.utcoffset() is not None,'time_invalid')
        return at.astimezone(timezone.utc)
    except (ValueError,TypeError,AttributeError):
        raise EvidenceBlocked('funding_forecasts_ref_time_invalid') from None


def _decoded(value):
    def pairs(items):
        result={}
        for key,item in items:
            _require(key not in result,'duplicate_json_key')
            result[key]=item
        return result
    try:
        if isinstance(value,str):
            return json.loads(value,object_pairs_hook=pairs,parse_constant=lambda _:(_require(False,'nonfinite')))
        return value
    except (ValueError,TypeError):raise EvidenceBlocked('funding_forecasts_ref_json_invalid') from None


def validate_forecasts_ref(ref,*,account_id):
    _require(isinstance(ref,dict) and set(ref)==_FIELDS,'shape_invalid')
    _require(ref['version']==VERSION and ref['table']==TABLE and ref['order']==ORDER
             and ref['canonical_contract']==CONTRACT,'contract_invalid')
    _require(isinstance(account_id,str) and account_id and ref['account_id']==account_id,'account_mismatch')
    for key in ('row_count','forecast_count'):
        _require(type(ref[key]) is int and ref[key]>=0,'count_invalid')
    _require(isinstance(ref['ordered_sha256'],str) and _HASH.fullmatch(ref['ordered_sha256']),'digest_invalid')
    cutoff=_time(ref['available_through'])
    _require(ref['available_through']==cutoff.isoformat(),'cutoff_not_canonical')
    for name in ('first_key','last_key'):
        key=ref[name]
        if ref['row_count']==0:_require(key is None,'empty_key_invalid')
        else:
            _require(isinstance(key,list) and len(key)==2 and isinstance(key[1],str) and _HASH.fullmatch(key[1]),'key_invalid')
            _require(_time(key[0]).isoformat()==key[0] and _time(key[0])<=cutoff,'key_invalid')
    return cutoff


def _summary(account_id,cutoff,rows,expected_count,*,collect=True):
    _require(isinstance(account_id,str) and bool(account_id),'account_mismatch')
    _require(type(expected_count) is int and expected_count>=0,'independent_count_invalid')
    cutoff=_time(cutoff)
    digest=hashlib.sha256();first=last=None;count=forecast_count=0;forecasts=[];seen=set()
    for raw in rows:
        row=dict(raw)
        _require(set(row)=={'account_id','evidence_sha256','available_at','payload'},'source_shape_invalid')
        _require(row['account_id']==account_id,'account_mismatch')
        at=_time(row['available_at'])
        _require(at<=cutoff,'source_after_cutoff')
        payload=_decoded(row['payload'])
        _require(isinstance(payload,dict) and set(payload)=={'forecasts'} and isinstance(payload['forecasts'],list)
                 and bool(payload['forecasts']),'source_payload_invalid')
        encoded,source_hash=canonical(payload)
        _require(isinstance(row['evidence_sha256'],str) and row['evidence_sha256']==source_hash,'source_hash_mismatch')
        key=[at.isoformat(),source_hash]
        order_key=(at,source_hash)
        _require(last is None or last<order_key,'source_order_or_duplicate')
        _require(source_hash not in seen,'source_identity_duplicate');seen.add(source_hash)
        if first is None:first=key
        last=order_key
        complete=dict(account_id=account_id,evidence_sha256=source_hash,available_at=at.isoformat(),payload=payload)
        framed=canonical(complete)[0].encode('utf-8')
        digest.update(len(framed).to_bytes(8,'big'));digest.update(framed)
        for forecast in payload['forecasts']:
            _require(isinstance(forecast,dict) and isinstance(forecast.get('symbol'),str) and bool(forecast['symbol']),'forecast_invalid')
            for name in ('source_time','received_at','next_funding_time'):_time(forecast.get(name))
            _require(_time(forecast['source_time'])<=_time(forecast['received_at'])<=at,'source_availability_invalid')
            if collect:forecasts.append(dict(forecast))
        forecast_count+=len(payload['forecasts']);count+=1
    _require(count==expected_count,'independent_count_mismatch')
    ref=dict(version=VERSION,table=TABLE,account_id=account_id,available_through=cutoff.isoformat(),row_count=count,
        forecast_count=forecast_count,order=ORDER,ordered_sha256=digest.hexdigest(),first_key=first,
        last_key=[last[0].isoformat(),last[1]] if last is not None else None,canonical_contract=CONTRACT)
    return ref,forecasts


def build_forecasts_ref(account_id,available_through,rows,*,expected_count):
    """Build from independently counted, already ordered complete source rows."""
    return _summary(account_id,available_through,rows,expected_count)


def resolve_forecasts_ref(ref,rows,*,account_id,expected_count,collect=True):
    """Verify the exact declared prefix. Extra rows are errors, never discarded."""
    cutoff=validate_forecasts_ref(ref,account_id=account_id)
    actual,forecasts=_summary(account_id,cutoff,rows,expected_count,collect=collect)
    _require(actual==ref,'closure_mismatch')
    return forecasts if collect else {key:actual[key] for key in ('row_count','forecast_count','ordered_sha256')}


def resolve_evidence_forecasts(parent,source_rows,*,account_id,expected_count,collect=True):
    """Validate RAW parent first, return a separate forecast view without mutation.

    source_rows must be the exact account/cutoff prefix, not a larger export's
    whole union. The caller independently counts that prefix in its source/index.
    """
    payload=_decoded(parent['payload'])
    _require(canonical(payload)[1]==parent['evidence_sha256'],'parent_hash_mismatch')
    _require(payload.get('account_id')==account_id and parent.get('account_id',account_id)==account_id,'account_mismatch')
    publication=payload.get('capture',{}).get('funding_publication',{})
    if 'forecasts_ref' in publication:
        _require('forecasts' not in publication,'ambiguous_inline_and_ref')
        cutoff=validate_forecasts_ref(publication['forecasts_ref'],account_id=account_id)
        _require(cutoff<=_time(payload.get('available_at')),'parent_availability_invalid')
        return resolve_forecasts_ref(publication['forecasts_ref'],source_rows,account_id=account_id,expected_count=expected_count,collect=collect)
    _require('forecasts' in publication and isinstance(publication['forecasts'],list),'missing')
    return copy.deepcopy(publication['forecasts']) if collect else dict(forecast_count=len(publication['forecasts']),legacy_inline=True)


async def read_forecasts_prefix(connection,account_id,available_through):
    """Caller owns one consistent snapshot/lease; SQL scope is fixed here."""
    cutoff=_time(available_through)
    expected=await connection.fetchval(COUNT_SQL,account_id,cutoff)
    rows=await connection.fetch(ROWS_SQL,account_id,cutoff)
    return build_forecasts_ref(account_id,cutoff,rows,expected_count=expected)


async def load_forecasts_ref(connection,ref,*,account_id,collect=True):
    cutoff=validate_forecasts_ref(ref,account_id=account_id)
    expected=await connection.fetchval(COUNT_SQL,account_id,cutoff)
    rows=await connection.fetch(ROWS_SQL,account_id,cutoff)
    return resolve_forecasts_ref(ref,rows,account_id=account_id,expected_count=expected,collect=collect)


async def verify_stored_evidence(connection,parent,*,account_id):
    """Validate the raw stored parent before reading its exact source closure."""
    payload=_decoded(parent['payload'])
    _require(canonical(payload)[1]==parent['evidence_sha256'],'parent_hash_mismatch')
    _require(payload.get('account_id')==account_id and parent.get('account_id',account_id)==account_id,'account_mismatch')
    publication=payload.get('capture',{}).get('funding_publication',{})
    if 'forecasts_ref' in publication:
        _require('forecasts' not in publication,'ambiguous_inline_and_ref')
        cutoff=validate_forecasts_ref(publication['forecasts_ref'],account_id=account_id)
        _require(cutoff<=_time(payload.get('available_at')),'parent_availability_invalid')
        return await load_forecasts_ref(connection,publication['forecasts_ref'],account_id=account_id,collect=False)
    _require('forecasts' in publication and isinstance(publication['forecasts'],list),'missing')
    return dict(forecast_count=len(publication['forecasts']),legacy_inline=True)
