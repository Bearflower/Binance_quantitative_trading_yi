"""候选扫描数据就绪回归：真实计算、逐截面门槛与本轮缓存。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from strategies.hrs.candidate_pool import CandidatePool
from strategies.hrs.market_data import MarketDataProvider


def market(samples_with_cap):
    async def cap(symbol, *_):
        valid = int(symbol.removesuffix('USDT')) < samples_with_cap
        return SimpleNamespace(source='coingecko' if valid else 'missing', value=1000 if valid else 0)
    return SimpleNamespace(
        get_funding_rate=AsyncMock(return_value=0.0001),
        get_oi_usd=AsyncMock(return_value=100),
        get_market_cap_provenance=cap,
        get_klines_1h=AsyncMock(return_value=[{'close': 100}]),
        get_ema20_4h=AsyncMock(return_value=100),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('total,cap_count,valid', [(38,30,True),(10,10,True),(9,9,False),(38,9,False)])
async def test_compute_checks_samples_before_completion_timestamp(total, cap_count, valid):
    pool = CandidatePool({'candidate_pool': {'dynamic_thresholds': {'enabled': True, 'min_sample_size': 10}}}, market(cap_count))
    tickers = {f'{i}USDT': {'quoteVolume': '10000', 'lastPrice': '110'} for i in range(total)}
    result = await pool._compute_dynamic_thresholds(tickers)
    assert result.is_valid() is valid
    assert result.funding_sample_count == total
    assert result.oi_market_cap_sample_count == cap_count
    if valid:
        assert result.computed_at is not None
        assert result.funding_rate_short == pytest.approx(10.95)
        assert result.oi_market_cap_short == pytest.approx(0.1)
        assert result.ema20_short == pytest.approx(0.1)
    else:
        assert result.computed_at is None
        assert result.funding_rate_short == 0


@pytest.mark.asyncio
async def test_scan_cache_reuses_success_but_not_empty_or_previous_scan():
    data = market(0)
    pool = CandidatePool({}, data)
    pool._scan_klines_1h_cache.set({})
    await pool._get_scan_klines_1h('AUSDT')
    await pool._get_scan_klines_1h('AUSDT')
    assert data.get_klines_1h.await_count == 1
    pool._scan_klines_1h_cache.set(None)
    await pool._get_scan_klines_1h('AUSDT')
    assert data.get_klines_1h.await_count == 2
    pool._scan_klines_1h_cache.set({})
    data.get_klines_1h.return_value = []
    await pool._get_scan_klines_1h('AUSDT')
    await pool._get_scan_klines_1h('AUSDT')
    assert data.get_klines_1h.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('size,level', [(100,'info'),(4,'warning'),(0,'warning')])
async def test_empty_cache_reports_actual_api_fallback_outcome(size, level):
    # 无需构造外部市值客户端，只测试现有 K 线读取路径。
    data = MarketDataProvider.__new__(MarketDataProvider)
    data.config = {'kline': {'min_klines_for_analysis': 24}}
    data.max_api_limit = 100
    data.kline_service = SimpleNamespace(get_klines=AsyncMock(return_value=[]))
    rows = [{'close': 100}] * size
    data.binance_api = SimpleNamespace(get_klines=AsyncMock(return_value=rows))
    with patch('strategies.hrs.market_data.logger') as log:
        assert await data.get_klines_1h('AUSDT') == rows
    getattr(log, level).assert_called_once()
    if level == 'info': log.warning.assert_not_called()
    data.binance_api.get_klines.assert_awaited_once_with(symbol='AUSDT', interval='1h', limit=100)


@pytest.mark.asyncio
async def test_scan_lifecycle_discards_cache_on_completion_and_error():
    data = market(1)
    data.get_all_tickers = AsyncMock(return_value=[{'symbol': '0USDT', 'quoteVolume': '10000', 'lastPrice': '110'}])
    data.preload_market_caps = AsyncMock()
    pool = CandidatePool({}, data)
    pool._load_new_coin_open_positions = AsyncMock()
    pool._should_exclude = lambda symbol: False
    pool._check_liquidity = lambda ticker: True

    async def count(symbol, ticker):
        await pool._get_scan_klines_1h(symbol)
        return 2
    pool._count_short_conditions = count
    pool._count_long_conditions = count
    await pool.scan_and_update()
    assert data.get_klines_1h.await_count == 1
    assert pool._scan_klines_1h_cache.get() is None
    await pool.scan_and_update()
    assert data.get_klines_1h.await_count == 2
    assert pool._scan_klines_1h_cache.get() is None
    data.get_all_tickers.side_effect = RuntimeError('行情不可用')
    await pool.scan_and_update()
    assert pool._scan_klines_1h_cache.get() is None


@pytest.mark.asyncio
async def test_concurrent_scan_contexts_do_not_share_klines():
    import asyncio
    data = market(0)
    pool = CandidatePool({}, data)
    first = asyncio.Event()
    second = asyncio.Event()
    async def reader(one):
        token = pool._scan_klines_1h_cache.set({})
        try:
            await pool._get_scan_klines_1h('AUSDT')
            (first if one else second).set()
            await (second if one else first).wait()
            await pool._get_scan_klines_1h('AUSDT')
        finally:
            pool._scan_klines_1h_cache.reset(token)
    await asyncio.gather(reader(True), reader(False))
    assert data.get_klines_1h.await_count == 2
    assert pool._scan_klines_1h_cache.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('cached_size,api_calls', [(23,1),(80,1),(100,0)])
async def test_partial_service_history_is_completed_from_api(cached_size, api_calls):
    data = MarketDataProvider.__new__(MarketDataProvider)
    data.config = {'kline': {'min_klines_for_analysis': 24}}
    data.max_api_limit = 100
    cached = [{'close': 100}] * cached_size
    direct = [{'close': 101}] * 100
    data.kline_service = SimpleNamespace(get_klines=AsyncMock(return_value=cached))
    data.binance_api = SimpleNamespace(get_klines=AsyncMock(return_value=direct))
    rows = await data.get_klines_1h('MANTRAUSDT', limit=168)
    assert len(rows) == 100
    assert data.binance_api.get_klines.await_count == api_calls
    assert rows[0]['close'] == (101 if api_calls else 100)
