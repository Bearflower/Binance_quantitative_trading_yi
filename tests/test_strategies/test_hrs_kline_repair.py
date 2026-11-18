"""Engineering fixtures preserve the exact five production timestamp gap shapes.

Production run d3f286aff0784292bd710a7779ff1e4a, 2026-11-18 08:21 UTC;
original analysis/outcome canonical hashes verified before extracting times.
Prices here are deliberately synthetic; these are repair mechanics, not backtests.
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from strategies.hrs.market_data import MarketDataProvider

HOUR = 3_600_000
AT = datetime(2026, 11, 18, 8, 21, tzinfo=timezone.utc)


def ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()*1000)


def bar(t):
    return dict(open_time=t, open=Decimal('1'), high=Decimal('2'),
                low=Decimal('.5'), close=Decimal('1.5'), volume=Decimal('3'))


def raw(t):
    return [t, '1', '2', '.5', '1.5', '3', t+HOUR-1, '4', 1, '1', '1', '0']


def provider(response=None):
    async def request(method, path, params, signed):
        assert method == 'GET' and path == '/fapi/v1/klines' and signed is False
        return [raw(t) for t in range(params['startTime'], params['endTime']+1, HOUR)][:params['limit']]
    client = SimpleNamespace(_request=AsyncMock(side_effect=response or request))
    obj = MarketDataProvider.__new__(MarketDataProvider)
    obj.binance_api = client
    obj.config = {'kline': {'keep_count':168,'synthetic_4h_count':50,'min_klines_for_analysis':24}}
    obj.max_api_limit = 100
    obj._synthetic_4h_interval = 4
    return obj


# (start, end, missing inclusive ranges): actual raw one-hour source timestamps.
PROFILES = [
    ('ARIAUSDT','2026-11-10T04:00','2026-11-17T11:00', [('2026-11-16T12:00','2026-11-16T19:00')], '2026-10-25T16:00',168),
    ('BTWUSDT','2026-11-13T16:00','2026-11-18T07:00', [('2026-11-16T12:00','2026-11-16T15:00'),('2026-11-17T08:00','2026-11-17T15:00')], '2026-11-13T16:00',100),
    ('PUMPUSDT','2026-11-01T18:00','2026-11-17T07:00', [('2026-11-04T14:00','2026-11-12T11:00'),('2026-11-16T00:00','2026-11-16T15:00')], '2026-10-31T08:00',168),
    ('EVAAUSDT','2026-11-09T23:00','2026-11-18T07:00', [('2026-11-10T00:00','2026-11-11T08:00'),('2026-11-12T01:00','2026-11-14T19:00'),('2026-11-17T12:00','2026-11-17T12:00')], '2026-11-09T20:00',100),
    ('MMTUSDT','2026-08-07T20:00','2026-11-18T07:00', [('2026-08-10T01:00','2026-08-10T19:00'),('2026-08-12T00:00','2026-11-17T15:00')], '2026-08-07T20:00',97),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol,start,end,gaps,start4,count',PROFILES)
async def test_repairs_real_five_gap_shapes_before_normal_window(symbol,start,end,gaps,start4,count):
    from strategies.hrs.core_decision import validate_klines
    missing = {t for a,b in gaps for t in range(ms(a),ms(b)+1,HOUR)}
    original = [bar(t) for t in range(ms(start),ms(end)+1,HOUR) if t not in missing]
    assert len(original)==count
    p = provider()
    result = await p.prepare_decision_klines(symbol, original, [bar(ms(start4))], decision_at=AT)
    assert original[0]['open_time']==ms(start)  # no mutation of historical capture
    validate_klines(result.klines_1h,AT,minimum=24)
    validate_klines(result.klines_4h,AT,interval_ms=4*HOUR,minimum=1)
    assert result.klines_1h[-1]['open_time']==ms('2026-11-18T07:00')
    assert result.receipt['full_start_time']==ms(start4)
    assert result.receipt['full_count']==(ms('2026-11-18T07:00')-ms(start4))//HOUR+1
    assert len(result.klines_1h)<=168 and len(result.klines_4h)<=50
    assert 1<=len(result.receipt['requests'])<=64
    assert result.available_at>=AT and all(r['available_at']<=result.available_at for r in result.receipt['requests'])
    assert result.receipt['input_1h_sha256']!=result.receipt['output_1h_sha256']


@pytest.mark.asyncio
async def test_incomplete_four_hour_children_never_become_complete_bar():
    p=provider()
    source=[bar(ms('2026-11-17T20:00')+i*HOUR) for i in range(5)]
    result=await p.synthesize_4h_klines(source)
    assert [b['open_time'] for b in result]==[ms('2026-11-17T20:00')]
    assert result[0]['volume']==12


@pytest.mark.asyncio
async def test_sort_identical_dedup_and_closed_only_without_network():
    p=provider()
    source=[bar(ms('2026-11-11T08:00')+i*HOUR) for i in range(168)]
    source=list(reversed(source))+[source[0],bar(ms('2026-11-18T08:00'))]
    result=await p.prepare_decision_klines('BTCUSDT',source,[],decision_at=AT)
    assert result.klines_1h[-1]['open_time']==ms('2026-11-18T07:00')
    assert len(result.klines_1h)==168
    assert result.receipt['dropped_unclosed_count']==1
    p.binance_api._request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('case,reason',[('conflict','kline_timestamp_conflict'),('span','kline_repair_span_exceeded'),('empty','kline_repair_incomplete'),('timeout','kline_repair_source_unavailable')])
async def test_fail_closed_with_structured_reason_and_original_hash(case,reason):
    from strategies.hrs.kline_repair import KlineHistoryUnavailable
    p=provider()
    original=[bar(ms('2026-11-17T00:00')+i*HOUR) for i in range(32)]
    if case=='conflict':
        original.append({**original[0],'close':Decimal('1.6')})
    elif case=='span':
        original.insert(0,bar(ms('2025-01-01T00:00')))
    else:
        original.pop(10)
        p.binance_api._request=AsyncMock(return_value=[]) if case=='empty' else AsyncMock(side_effect=TimeoutError())
    with pytest.raises(KlineHistoryUnavailable) as err:
        await p.prepare_decision_klines('BTCUSDT',original,[],decision_at=AT)
    assert err.value.reason==reason
    assert err.value.receipt['input_1h_sha256']


@pytest.mark.asyncio
async def test_unexpected_programming_error_is_not_data_unavailable():
    p=provider()
    p.binance_api._request=AsyncMock(side_effect=RuntimeError('unexpected'))
    with pytest.raises(RuntimeError,match='unexpected'):
        await p.prepare_decision_klines('BTCUSDT',[bar(ms('2026-11-17T00:00'))],[],decision_at=AT)


@pytest.mark.asyncio
@pytest.mark.parametrize('case,reason',[
    ('wrong_time','kline_repair_response_out_of_bounds'),
    ('missing_middle','kline_repair_incomplete'),
    ('duplicate_conflict','kline_timestamp_conflict'),
    ('malformed','kline_repair_response_invalid'),
    ('insufficient','kline_warmup_insufficient'),
    ('request_limit','kline_repair_request_limit'),
])
async def test_real_response_validation_never_accepts_partial_repair(case,reason,monkeypatch):
    from strategies.hrs.kline_repair import KlineHistoryUnavailable
    source=[bar(ms('2026-11-17T00:00')+i*HOUR) for i in range(32)]
    source=source[:8]+source[12:]
    async def bad(method,path,params,signed):
        times=list(range(params['startTime'],params['endTime']+1,HOUR))
        if case=='wrong_time': return [raw(times[0]-HOUR)]
        if case=='missing_middle': return [raw(times[0]),raw(times[-1])]
        if case=='malformed': return {'code':0}
        if case=='duplicate_conflict':
            a=raw(times[0]); b=list(a); b[4]='1.6'; return [a,b]
        return [raw(t) for t in times]
    if case=='insufficient': source=[bar(ms('2026-11-18T04:00')+i*HOUR) for i in range(4)]
    if case=='request_limit':
        monkeypatch.setattr('strategies.hrs.kline_repair.MAX_REQUESTS',1)
        source=source[:-2]+[source[-1]]
    p=provider(bad)
    with pytest.raises(KlineHistoryUnavailable) as err:
        await p.prepare_decision_klines('BTCUSDT',source,[],decision_at=AT)
    assert err.value.reason==reason
    assert err.value.receipt['status']=='unavailable'


@pytest.mark.asyncio
async def test_receipt_binds_raw_responses_and_final_windows_with_capture_hashes():
    from strategies.hrs.decision_capture import canonical_payload
    original=[bar(ms('2026-11-17T00:00')+i*HOUR) for i in range(32)]
    original.pop(10)
    p=provider()
    result=await p.prepare_decision_klines('BTCUSDT',original,[],decision_at=AT)
    receipt=result.receipt
    assert receipt['input_1h_sha256']==canonical_payload(original)[1]
    assert receipt['output_1h_sha256']==canonical_payload(result.klines_1h)[1]
    assert receipt['output_4h_sha256']==canonical_payload(result.klines_4h)[1]
    request=receipt['requests'][0]
    assert request['response_sha256']==canonical_payload([raw(ms('2026-11-17T10:00'))])[1]
    assert request['params']['endTime']==ms('2026-11-17T11:00')-1


@pytest.mark.asyncio
async def test_total_wall_clock_budget_cancels_slow_public_repair(monkeypatch):
    import asyncio
    from strategies.hrs.kline_repair import KlineHistoryUnavailable
    cancelled=[]
    async def slow(*args,**kwargs):
        try: await asyncio.sleep(1)
        finally: cancelled.append(True)
    p=provider(slow)
    monkeypatch.setattr('strategies.hrs.kline_repair.REPAIR_TIMEOUT_SECONDS',.01)
    source=[bar(ms('2026-11-17T00:00')+i*HOUR) for i in range(32)]
    source.pop(10)
    with pytest.raises(KlineHistoryUnavailable,match='kline_repair_budget_exceeded') as err:
        await p.prepare_decision_klines('BTCUSDT',source,[],decision_at=AT)
    assert cancelled==[True]
    assert err.value.receipt['requests'][0]['error_type']=='TimeoutError'
