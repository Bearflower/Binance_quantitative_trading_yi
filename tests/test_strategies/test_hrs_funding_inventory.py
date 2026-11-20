"""Prospective public inventory capture is source evidence, never performance."""
import copy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import pytest
from strategies.hrs.decision_capture import canonical_payload

AT=datetime(2026,11,20,tzinfo=timezone.utc)

def responses():
    def row(symbol,next_time):
        return dict(symbol=symbol,time=int(AT.timestamp()*1000),nextFundingTime=next_time,markPrice='10')
    return [row('A',int((AT+timedelta(hours=1)).timestamp()*1000)),
        row('B',int((AT+timedelta(hours=2)).timestamp()*1000)),row('OLD',0)], [dict(symbol='A',fundingIntervalHours=1)]

async def captured():
    from strategies.hrs.funding_inventory import FundingInventoryCollector
    premium,info=responses();api=SimpleNamespace(_request=AsyncMock(side_effect=[premium,info]))
    recorder=SimpleNamespace(record=AsyncMock(return_value='durable-id'),failed=False)
    collector=FundingInventoryCollector(api,recorder,account_id='test')
    times=iter([AT,AT+timedelta(seconds=1),AT+timedelta(seconds=2),AT+timedelta(seconds=3)])
    collector.clock=lambda:next(times)
    result=await collector.collect_once()
    call=recorder.record.await_args
    encoded,digest=canonical_payload(call.args[2])
    row=dict(evidence_id='inventory',run_id=call.args[0],kind=call.args[1],symbol=None,
        event_time=call.kwargs['as_of'],available_at=call.kwargs['available_at'],payload=json.loads(encoded),payload_sha256=digest)
    return collector,api,recorder,result,row

@pytest.mark.asyncio
async def test_complete_inventory_keeps_original_clocks_hashes_and_unknown_interval():
    from strategies.hrs.funding_inventory import inventory_observations
    collector,api,recorder,result,row=await captured()
    assert result['status']=='complete'
    assert row['kind']=='funding_schedule_inventory'
    assert row['event_time']==AT and row['available_at']>AT
    assert [call.args[:2] for call in api._request.await_args_list]==[('GET','/fapi/v1/premiumIndex'),('GET','/fapi/v1/fundingInfo')]
    assert all(call.kwargs['signed'] is False for call in api._request.await_args_list)
    assert row['payload']['ignored_symbols']==[dict(symbol='OLD',reason='next_funding_time_not_prospective')]
    observations=inventory_observations(row,account_id='test')
    assert [o['symbol'] for o in observations]==['A','B']
    assert observations[0]['funding_interval_hours']==1
    assert observations[1]['funding_interval_hours'] is None
    assert observations[0]['received_at']==(AT+timedelta(seconds=2)).isoformat()
    assert row['payload']['premium_index']['response']==responses()[0]

@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['envelope_hash','normalized','raw','account','clock'])
async def test_inventory_source_or_normalization_tampering_is_denied(damage):
    from strategies.hrs.funding_inventory import inventory_observations
    *_,row=await captured();row=copy.deepcopy(row)
    if damage=='envelope_hash':row['payload_sha256']='0'*64
    elif damage=='normalized':row['payload']['observations'][0]['next_funding_time']=(AT+timedelta(hours=3)).isoformat()
    elif damage=='raw':row['payload']['premium_index']['response'][0]['nextFundingTime']+=1000
    elif damage=='account':row['payload']['account_id']='other'
    else:row['payload']['received_at']=(AT-timedelta(seconds=1)).isoformat()
    if damage!='envelope_hash':row['payload_sha256']=canonical_payload(row['payload'])[1]
    with pytest.raises(ValueError,match='funding_inventory'):
        inventory_observations(row,account_id='test')

@pytest.mark.asyncio
async def test_get_failure_records_incomplete_evidence_without_observations():
    from strategies.hrs.funding_inventory import FundingInventoryCollector,inventory_observations
    api=SimpleNamespace(_request=AsyncMock(side_effect=TimeoutError('unavailable')))
    recorder=SimpleNamespace(record=AsyncMock(),failed=False)
    collector=FundingInventoryCollector(api,recorder,account_id='test');collector.clock=lambda:AT
    result=await collector.collect_once()
    assert result['status']=='failed' and 'observations' not in recorder.record.await_args.args[2]
    payload=recorder.record.await_args.args[2]
    row=dict(kind='funding_schedule_inventory',payload=payload,payload_sha256=canonical_payload(payload)[1],event_time=AT,available_at=AT)
    assert inventory_observations(row,account_id='test')==[]
    assert recorder.failed is False

@pytest.mark.asyncio
async def test_durable_writer_failure_propagates_and_blocks_new_risk():
    from strategies.hrs.funding_inventory import FundingInventoryCollector
    api=SimpleNamespace(_request=AsyncMock(side_effect=list(responses())))
    recorder=SimpleNamespace(record=AsyncMock(side_effect=RuntimeError('writer')),failed=False)
    collector=FundingInventoryCollector(api,recorder,account_id='test');collector.clock=lambda:AT
    with pytest.raises(RuntimeError,match='writer'):await collector.collect_once()
    assert recorder.failed is True

@pytest.mark.asyncio
async def test_expired_token_symbol_survives_canonical_secret_key_filter():
    from strategies.hrs.funding_inventory import FundingInventoryCollector,inventory_observations
    premium,info=responses();premium[-1]['symbol']='TOKENUSDT'
    api=SimpleNamespace(_request=AsyncMock(side_effect=[premium,info]));recorder=SimpleNamespace(record=AsyncMock(),failed=False)
    collector=FundingInventoryCollector(api,recorder,account_id='test');collector.clock=lambda:AT
    await collector.collect_once();call=recorder.record.await_args;encoded,digest=canonical_payload(call.args[2])
    row=dict(kind=call.args[1],payload=json.loads(encoded),payload_sha256=digest,event_time=AT,available_at=AT)
    assert len(inventory_observations(row,account_id='test'))==2
    assert {'symbol':'TOKENUSDT','reason':'next_funding_time_not_prospective'} in row['payload']['ignored_symbols']

@pytest.mark.asyncio
async def test_failed_timeout_batch_retains_elapsed_clock_without_forecasts():
    from strategies.hrs.funding_inventory import FundingInventoryCollector,inventory_observations
    api=SimpleNamespace(_request=AsyncMock(side_effect=TimeoutError('elapsed')));recorder=SimpleNamespace(record=AsyncMock(),failed=False)
    collector=FundingInventoryCollector(api,recorder,account_id='test');times=iter([AT,AT+timedelta(seconds=120.001)])
    collector.clock=lambda:next(times)
    result=await collector.collect_once();call=recorder.record.await_args;encoded,digest=canonical_payload(call.args[2])
    row=dict(kind=call.args[1],payload=json.loads(encoded),payload_sha256=digest,event_time=AT,available_at=AT+timedelta(seconds=120.001))
    assert result['status']=='failed'
    assert inventory_observations(row,account_id='test')==[]

@pytest.mark.asyncio
async def test_background_loop_waits_after_startup_collection_and_closes_without_duplicate_gets():
    import asyncio
    collector,api,recorder,*_=await captured()
    task=asyncio.create_task(collector.run())
    await asyncio.sleep(0)
    await collector.close();await task
    assert api._request.await_count==2
    assert recorder.record.await_count==1

@pytest.mark.asyncio
async def test_bound_inventory_view_never_uses_a_batch_not_available_at_opening():
    from ai_tuner.hrs_evidence_export import _FundingInventoryView
    *_,row=await captured()
    scope=dict(account_id='test',symbols=['A'],window_start=(AT+timedelta(seconds=2.5)).isoformat(),as_of=(AT+timedelta(seconds=4)).isoformat())
    view=_FundingInventoryView([], [row],scope)
    try:assert len(view)==0
    finally:view.close()

@pytest.mark.asyncio
async def test_bound_inventory_view_rejects_conflicting_original_observation_identity():
    import hashlib
    from ai_tuner.hrs_evidence_export import _FundingInventoryView
    from ai_tuner.replay.models import DataBlocked
    from strategies.hrs.funding_inventory import inventory_observations
    *_,row=await captured();original=inventory_observations(row,account_id='test')[0];conflict=dict(original,next_funding_time=(AT+timedelta(hours=3)).isoformat())
    selected=dict(symbol='A',time=int(AT.timestamp()*1000),nextFundingTime=int((AT+timedelta(hours=3)).timestamp()*1000),fundingIntervalHours=1)
    conflict['payload_hash']=hashlib.sha256(json.dumps(selected,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    scope=dict(account_id='test',symbols=['A'],window_start=(AT+timedelta(seconds=3)).isoformat(),as_of=(AT+timedelta(seconds=4)).isoformat())
    with pytest.raises(DataBlocked,match='funding_inventory_observation_conflict'):
        _FundingInventoryView([conflict],[row],scope)
