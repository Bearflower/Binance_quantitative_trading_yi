"""Prospective public funding inventories, independent of trading admission.

One immutable batch retains current public inputs for every returned contract.
Unknown intervals remain null. No order, policy, financial-ledger or notification
interface is used. A failed batch cannot supply a forecast or repair old history.
"""
from __future__ import annotations
import asyncio
from datetime import datetime,timedelta,timezone
import json
import time
import uuid
import structlog

log=structlog.get_logger()

from ai_tuner.funding_schedule.models import normalize_observations
from .decision_capture import canonical_payload

VERSION='hrs_funding_inventory_v1'
KIND='funding_schedule_inventory'
MAX_SYMBOLS=1500
MAX_BYTES=4*1024*1024
INTERVAL_SECONDS=300


def _time(value):
    if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('funding_inventory_clock_invalid')
    return value.astimezone(timezone.utc)


def _normalize(premiums,info,requested,received):
    if (not isinstance(premiums,list) or not 0<len(premiums)<=MAX_SYMBOLS
            or not isinstance(info,list) or len(info)>MAX_SYMBOLS
            or len(canonical_payload([premiums,info])[0].encode())>MAX_BYTES):
        raise ValueError('funding_inventory_response_unbounded')
    found=set();required=[];ignored=[]
    for row in premiums:
        symbol=row.get('symbol') if isinstance(row,dict) else None
        if not isinstance(symbol,str) or not symbol or symbol!=symbol.upper() or len(symbol)>32 or symbol in found:
            raise ValueError('funding_inventory_symbol_unproven')
        found.add(symbol)
        due=row.get('nextFundingTime')
        if type(due) is not int or due<=int(received.timestamp()*1000):
            ignored.append(dict(symbol=symbol,reason='next_funding_time_not_prospective'))
        else:required.append(symbol)
    if not required:raise ValueError('funding_inventory_no_prospective_symbols')
    digest,observations=normalize_observations(scheduled_at=requested,received_at=received,
        required_symbols=required,premium_rows=premiums,funding_info_rows=info)
    rows=[dict(symbol=o.symbol,source_time=o.source_time.isoformat(),received_at=o.received_at.isoformat(),
        next_funding_time=o.next_funding_time.isoformat(),funding_interval_hours=o.funding_interval_hours,
        payload_hash=o.payload_hash) for o in observations]
    return digest,rows,ignored


def inventory_observations(row,*,account_id):
    """Reproduce selected facts from the original immutable batch, never hydrate it."""
    try:
        payload=json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
        if (row.get('kind')!=KIND or canonical_payload(payload)[1]!=row.get('payload_sha256')
                or payload.get('version')!=VERSION or payload.get('account_id')!=account_id):
            raise ValueError('identity_or_hash')
        requested,received,available=map(_time,(payload['requested_at'],payload['received_at'],payload['available_at']))
        event,envelope=map(_time,(row['event_time'],row['available_at']))
        if not event==requested<=received<=available==envelope:
            raise ValueError('envelope_clock')
        if payload.get('status')=='failed':
            if 'observations' in payload:raise ValueError('failed_batch_has_forecasts')
            return []
        if payload.get('status')!='complete' or available-requested>timedelta(seconds=120):raise ValueError('status_or_freshness')
        premium,info=payload['premium_index'],payload['funding_info']
        if (premium['path']!='/fapi/v1/premiumIndex' or info['path']!='/fapi/v1/fundingInfo'
                or premium['params']!={} or info['params']!={}
                or not requested<=_time(payload['premium_received_at'])<=received
                or canonical_payload(premium['response'])[1]!=premium['response_sha256']
                or canonical_payload(info['response'])[1]!=info['response_sha256']):
            raise ValueError('original_public_source')
        digest,observations,ignored=_normalize(premium['response'],info['response'],requested,received)
        if (digest!=payload['normalized_sha256'] or observations!=payload['observations']
                or ignored!=payload['ignored_symbols'] or payload['raw_symbol_count']!=len(premium['response'])):
            raise ValueError('normalization_mismatch')
        return observations
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError) as exc:
        raise ValueError('funding_inventory_unproven') from exc


class FundingInventoryCollector:
    def __init__(self,api,recorder,account_id='default'):
        if not isinstance(account_id,str) or not account_id:raise ValueError('funding_inventory_account_required')
        self.api,self.recorder,self.account_id=api,recorder,account_id
        self.clock=lambda:datetime.now(timezone.utc)
        self._closed=asyncio.Event();self._last_started=None
        self.run_id='hrs-funding-inventory-'+uuid.uuid4().hex

    async def collect_once(self):
        requested=_time(self.clock());self._last_started=time.monotonic()
        payload=dict(version=VERSION,account_id=self.account_id,requested_at=requested)
        async def fetch():
            premium=await self.api._request('GET','/fapi/v1/premiumIndex',params={},signed=False)
            premium_received=_time(self.clock())
            info=await self.api._request('GET','/fapi/v1/fundingInfo',params={},signed=False)
            received=_time(self.clock())
            digest,observations,ignored=_normalize(premium,info,requested,received)
            available=_time(self.clock())
            if not requested<=premium_received<=received<=available or available-requested>timedelta(seconds=120):
                raise ValueError('funding_inventory_capture_stale')
            return dict(payload,status='complete',premium_received_at=premium_received,received_at=received,available_at=available,
                source='binance_um_public_forecast',raw_symbol_count=len(premium),ignored_symbols=ignored,
                premium_index=dict(path='/fapi/v1/premiumIndex',params={},response=premium,response_sha256=canonical_payload(premium)[1]),
                funding_info=dict(path='/fapi/v1/fundingInfo',params={},response=info,response_sha256=canonical_payload(info)[1]),
                normalized_sha256=digest,observations=observations)
        try:
            payload=await asyncio.wait_for(fetch(),timeout=120)
        except asyncio.CancelledError:raise
        except Exception as exc:
            available=_time(self.clock())
            payload.update(status='failed',received_at=available,available_at=available,failure_type=type(exc).__name__)
        try:
            await self.recorder.record(self.run_id,KIND,payload,as_of=requested,available_at=payload['available_at'])
        except BaseException:
            self.recorder.failed=True
            raise
        summary=dict(status=payload['status'],received_at=payload['received_at'],observations=len(payload.get('observations',())))
        if payload['status']=='complete':log.info('hrs_funding_inventory_captured',observations=summary['observations'])
        else:log.warning('hrs_funding_inventory_failed',failure_type=payload.get('failure_type'))
        return summary

    async def run(self):
        while not self._closed.is_set():
            delay=0 if self._last_started is None else max(0,INTERVAL_SECONDS-(time.monotonic()-self._last_started))
            if delay:
                try:await asyncio.wait_for(self._closed.wait(),timeout=delay)
                except asyncio.TimeoutError:pass
            if not self._closed.is_set():await self.collect_once()

    async def close(self):
        self._closed.set()
