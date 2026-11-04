from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,MagicMock
import pytest
from shared.market_cap import MarketCapService

@pytest.mark.asyncio
@pytest.mark.parametrize('method,args',[('_ensure_coin_list',()),('_fetch_coins_markets',(['ethereum'],)),('_fetch_market_cap_once',('ETHUSDT','ethereum'))])
async def test_rate_limit_entered_while_waiting_suppresses_http(method,args):
    service=MarketCapService();service.session=MagicMock()
    service._init_session=AsyncMock()
    service._is_rate_limited=lambda: False
    async def wait():service._is_rate_limited=lambda: True
    service._enforce_request_interval=wait
    await getattr(service,method)(*args)
    service.session.get.assert_not_called()

@pytest.mark.asyncio
async def test_hrs_lv_scan_preloads_market_caps_before_per_symbol_checks():
    from strategies.hrs.strategy import HRSStrategy
    strategy=object.__new__(HRSStrategy);strategy.config={}
    strategy.market_data=NS(preload_market_caps=AsyncMock())
    def already_standard():
        strategy.market_data.preload_market_caps.assert_awaited_once_with(['ETHUSDT'])
        return ['ETHUSDT']
    strategy.candidate_pool=NS(get_short_candidates=already_standard)
    await strategy._check_lv_rm_entries(['ETHUSDT'],[],[])
    strategy.market_data.preload_market_caps.assert_awaited_once_with(['ETHUSDT'])
