import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.store import DecisionStore, migrate


@pytest.mark.asyncio
async def test_new_order_ack_does_not_repeat_account_rest_reconciliation(tmp_path):
    path = tmp_path / 'journal'; migrate(path); store = DecisionStore(path)
    epoch = store.create_epoch('d')
    store.prepare_order(epoch, 0, 'ENTRY', 'BUY', Decimal(1600), Decimal('.013'), 'eg-entry')
    store.record_order('eg-entry', '1', 'NEW', Decimal(0));store.set_engine_state('ACTIVE')
    exchange = AsyncMock()
    engine = GridEngine(store, exchange)
    event = {'e':'ORDER_TRADE_UPDATE','o':dict(s='ETHUSDT',c='eg-entry',i=1,S='BUY',
        x='NEW',X='NEW',l='0',z='0',L='0',t=0)}
    assert await engine.on_order_update(event,epoch_id=epoch,levels=(),filters=None,
        stop_loss_low=Decimal(1500),stop_loss_high=Decimal(1900))
    exchange.position.assert_not_awaited()
    exchange.open_orders.assert_not_awaited()
    assert store.get_order('eg-entry')['status']=='NEW'
    store.close()


@pytest.mark.asyncio
async def test_weighted_budget_waits_for_full_window_and_honors_retry_after():
    from strategies.grid.execution.request_budget import RequestBudget
    now=[0.0]; waits=[]
    async def sleep(delay): waits.append(delay);now[0]+=delay
    budget=RequestBudget(limit=100,clock=lambda:now[0],sleep=sleep)
    await budget.acquire(80);await budget.acquire(40)
    assert sum(waits)>=60
    budget.observe(429,{'Retry-After':'75','X-MBX-USED-WEIGHT-1M':'2400'})
    before=now[0];await budget.acquire(1)
    assert now[0]-before>=75


def test_weights_count_account_wide_queries_and_trade_history():
    from strategies.grid.execution.request_budget import request_weight
    assert request_weight('GET','/fapi/v1/openOrders',None)==40
    assert request_weight('GET','/fapi/v1/openAlgoOrders',None)==40
    assert request_weight('GET','/fapi/v1/openOrders',{'symbol':'ETHUSDT'})==1
    assert request_weight('GET','/fapi/v1/income',{})==30


@pytest.mark.asyncio
async def test_ip_headers_and_ban_pause_requests_even_below_local_budget():
    from strategies.grid.execution.request_budget import RequestBudget
    now=[0.0]
    async def sleep(delay): now[0]+=delay
    b=RequestBudget(clock=lambda:now[0],sleep=sleep)
    b.observe(200,{'X-MBX-USED-WEIGHT-1M':'1800'})
    await b.acquire(1);assert now[0]>=60
    b.observe(418,{})
    await b.acquire(1);assert now[0]>=180


@pytest.mark.asyncio
async def test_single_post_still_never_retries_after_rate_error(monkeypatch):
    from strategies.grid.execution.request_budget import GridBinanceClient
    from shared.binance_api import BinanceClient,BinanceAPIError
    call=AsyncMock(side_effect=BinanceAPIError(-1003,'rate limit'))
    monkeypatch.setattr(BinanceClient,'_request_once',call)
    c=GridBinanceClient('test','test',use_unified_account=False)
    with pytest.raises(BinanceAPIError):
        await c._request_once('POST','/fapi/v1/order',{})
    call.assert_awaited_once()
    assert c.request_budget.cooldown_until>c.request_budget.clock()
