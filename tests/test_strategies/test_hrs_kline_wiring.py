from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
import pytest

from strategies.hrs.strategy import HRSStrategy
from strategies.hrs.kline_repair import PreparedKlines,KlineHistoryUnavailable


def setup_strategy(monkeypatch, failure=None):
    import strategies.hrs.strategy as module
    at=datetime.now(timezone.utc)
    hour=int(at.timestamp())//3600*3600000
    bar=lambda t:dict(open_time=t,open=10,high=11,low=9,close=10,volume=100)
    original=[bar(hour-4*3600000),bar(hour-2*3600000)]
    repaired=[bar(hour-2*3600000),bar(hour-3600000)]
    four=[bar((hour//14400000-1)*14400000)]
    receipt=dict(version='hrs_kline_repair_v1',symbol='TEST',status='complete',requests=[])
    prepared=PreparedKlines(repaired,four,at,receipt)
    provider=SimpleNamespace(prepare_decision_klines=AsyncMock(side_effect=failure) if failure else AsyncMock(return_value=prepared),
        get_oi_usd=AsyncMock(return_value=100),get_funding_rate=AsyncMock(return_value=0),get_24h_volume=AsyncMock(return_value=1000))
    obj=object.__new__(HRSStrategy);obj._klines_cache={'TEST':original};obj._klines_4h_cache={'TEST':[]}
    obj.market_data=provider;obj.min_klines=2;obj.kline_interval='1h';obj.config={}
    obj.scoring_engine=Mock();obj.pattern_recognizer=Mock()
    obj.candidate_pool=SimpleNamespace(get_short_candidates=lambda:['TEST'],get_long_candidates=lambda:[])
    obj._fetch_market_cap_ratio=AsyncMock(return_value=(.01,True,'coingecko',10000))
    obj._warmup_klines=AsyncMock(return_value=[])
    scorer=Mock(return_value={'symbol':'TEST','short':{'score_result':{}},'long':None})
    monkeypatch.setattr(module,'analyze_standard',scorer)
    stages=AsyncMock();monkeypatch.setattr(module,'record_stage',stages)
    logger=Mock();monkeypatch.setattr(module,'logger',logger)
    return obj,provider,prepared,scorer,stages,logger,original


@pytest.mark.asyncio
async def test_standard_scores_repaired_arrays_and_retains_original_receipt(monkeypatch):
    obj,provider,prepared,scorer,stages,logger,original=setup_strategy(monkeypatch)
    result=await obj.analyze('TEST')
    assert not result.get('error')
    provider.prepare_decision_klines.assert_awaited_once()
    assert scorer.call_args.kwargs['klines_1h']==prepared.klines_1h
    assert scorer.call_args.kwargs['klines_4h']==prepared.klines_4h
    assert obj._klines_cache['TEST']==prepared.klines_1h
    records=[call.args[2] for call in stages.await_args_list if call.args[1]=='kline_repair']
    assert records and records[0]['input_klines_1h']==original
    assert records[0]['input_klines_4h']==[] and records[0]['receipt']==prepared.receipt


@pytest.mark.asyncio
async def test_unavailable_bars_skip_with_reason_without_replacing_cache(monkeypatch):
    receipt={'version':'hrs_kline_repair_v1','status':'unavailable','reason':'kline_repair_incomplete'}
    obj,provider,prepared,scorer,stages,logger,original=setup_strategy(monkeypatch,KlineHistoryUnavailable('kline_repair_incomplete',receipt))
    result=await obj.analyze('TEST')
    assert result.get('skip') is True and result.get('reason')=='kline_repair_incomplete'
    assert obj._klines_cache['TEST']==original and obj._klines_4h_cache['TEST']==[]
    scorer.assert_not_called();logger.error.assert_not_called()
    assert any(c.args[1]=='kline_repair' and c.args[2]['receipt']==receipt for c in stages.await_args_list)


@pytest.mark.asyncio
async def test_unknown_repair_error_remains_a_real_error(monkeypatch):
    obj,provider,prepared,scorer,stages,logger,original=setup_strategy(monkeypatch,RuntimeError('implementation broken'))
    result=await obj.analyze('TEST')
    assert result.get('error')=='implementation broken'
    logger.error.assert_called_once();scorer.assert_not_called()
    assert obj._klines_cache['TEST']==original


@pytest.mark.asyncio
async def test_capture_keeps_the_arrays_actually_scored_when_cache_changes(monkeypatch):
    obj,provider,prepared,scorer,stages,logger,original=setup_strategy(monkeypatch)
    record=AsyncMock()
    obj.decision_recorder=SimpleNamespace(record=record,run_id='scan',failed=False)
    async def concurrent_update(symbol):
        obj._klines_cache[symbol]=[dict(prepared.klines_1h[-1],close=10.5)]
        obj._klines_4h_cache[symbol]=[]
        return 100
    provider.get_oi_usd.side_effect=concurrent_update
    result=await obj.analyze('TEST')
    payload=next(c.args[2] for c in record.await_args_list if c.args[1]=='analysis')
    assert scorer.call_args.kwargs['klines_1h']==prepared.klines_1h
    assert payload['klines_1h']==prepared.klines_1h and payload['klines_4h']==prepared.klines_4h
    assert '_audit_input_klines' not in result

@pytest.mark.asyncio
async def test_failed_analysis_keeps_actual_inputs_after_concurrent_cache_update(monkeypatch):
    obj,provider,prepared,scorer,stages,logger,original=setup_strategy(monkeypatch)
    record=AsyncMock()
    obj.decision_recorder=SimpleNamespace(record=record,run_id='scan',failed=False)
    async def concurrent_update(symbol):
        obj._klines_cache[symbol]=[dict(prepared.klines_1h[-1],close=10.5)]
        obj._klines_4h_cache[symbol]=[]
        return 100
    provider.get_oi_usd.side_effect=concurrent_update
    scorer.side_effect=RuntimeError('actual core failure')
    result=await obj.analyze('TEST')
    payload=next(c.args[2] for c in record.await_args_list if c.args[1]=='analysis')
    assert result['error']=='actual core failure'
    assert payload['klines_1h']==prepared.klines_1h
    assert payload['klines_4h']==prepared.klines_4h
    assert '_audit_input_klines' not in result

@pytest.mark.asyncio
async def test_failed_lv_analysis_keeps_actual_inputs_after_cache_update(monkeypatch):
    obj,provider,prepared,scorer,stages,logger,original=setup_strategy(monkeypatch)
    record=AsyncMock()
    obj.decision_recorder=SimpleNamespace(record=record,run_id='scan',failed=False)
    obj.candidate_pool=SimpleNamespace(get_short_candidates=lambda:[],get_long_candidates=lambda:[])
    obj.risk_manager=SimpleNamespace(is_circuit_breaker_active=lambda symbol:False)
    provider.preload_market_caps=AsyncMock()
    async def concurrent_update(symbol):
        obj._klines_cache[symbol]=[dict(prepared.klines_1h[-1],close=10.5)]
        obj._klines_4h_cache[symbol]=[]
        return 100
    provider.get_oi_usd.side_effect=concurrent_update
    obj.scoring_engine.score_lv_rm=AsyncMock(side_effect=RuntimeError('actual LV failure'))
    await obj._check_lv_rm_entries(['TEST'],[],[])
    payload=next(c.args[2] for c in record.await_args_list if c.args[1]=='lv_rm_candidate_outcome')
    assert payload['reason']=='analysis_error'
    assert payload['klines_1h']==prepared.klines_1h
    assert payload['klines_4h']==prepared.klines_4h
    logger.error.assert_called_once()
