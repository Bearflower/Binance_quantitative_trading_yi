from unittest.mock import AsyncMock,MagicMock
import pytest
from strategies.hrs.strategy import HRSStrategy


@pytest.mark.asyncio
async def test_union_batch_precedes_standard_scoring():
    strategy=HRSStrategy({'strategy': {'name': 'hrs'}})
    strategy._is_settlement_skip_window=MagicMock(return_value=False)
    strategy.binance_client=MagicMock(get_account_info=AsyncMock(return_value={'totalMarginBalance':'1'}))
    strategy.risk_manager=MagicMock()
    strategy.risk_manager.check_drawdown=AsyncMock(return_value=True)
    strategy.risk_manager.is_paused.return_value=False
    strategy.risk_manager.is_circuit_breaker_active.return_value=False
    strategy.risk_manager.check_price_change.return_value=True
    strategy._check_candidate_update=AsyncMock()
    strategy.candidate_pool=MagicMock()
    strategy.candidate_pool.get_active_symbols.return_value={'BTCUSDT'}
    strategy.candidate_pool.get_lv_rm_scan_range=AsyncMock(return_value=['ETHUSDT','BTCUSDT'])
    strategy.market_data=MagicMock()
    events=[]
    async def preload(symbols):events.append(('preload',symbols))
    strategy.market_data.preload_market_caps=preload
    async def analyze(symbol):events.append(('analyze',symbol));return None
    strategy.analyze=analyze
    strategy._monitor_positions=AsyncMock()
    strategy._report_positions_to_dashboard=AsyncMock()
    await strategy._execute_cycle()
    assert events[0]==('preload',['BTCUSDT','ETHUSDT'])
    assert any(e[0]=='analyze' for e in events)
