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


def test_batch_admission_waits_before_first_order_without_consuming_quota():
    from strategies.grid.execution.request_budget import RequestBudget
    now=[0.0];b=RequestBudget(clock=lambda:now[0])
    b.used.append((0,400))
    assert not b.has_capacity(960)
    assert b.has_capacity(800)
    now[0]=60
    assert b.has_capacity(960) and not b.used
    b.observe(429,{})
    assert not b.has_capacity(1)


@pytest.mark.asyncio
async def test_flat_position_mode_check_is_bounded_and_expires(monkeypatch):
    from strategies.grid.execution.exchange import ClassicGridExchange
    from types import SimpleNamespace
    now=[0.0]
    monkeypatch.setattr('strategies.grid.execution.exchange.time.monotonic',lambda:now[0])
    async def request(method,path,*args,**kwargs):
        return {'dualSidePosition':False} if path.endswith('/dual') else []
    client=SimpleNamespace(use_unified_account=False,_request=AsyncMock(side_effect=request))
    x=ClassicGridExchange(client)
    for _ in range(10):assert (await x.position())[0]['positionAmt']=='0'
    assert sum(c.args[1].endswith('/dual') for c in client._request.await_args_list)==1
    now[0]=31
    await x.position()
    assert sum(c.args[1].endswith('/dual') for c in client._request.await_args_list)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_used', [0, 500])
async def test_classic_flat_eleven_order_start_fits_one_weight_window(tmp_path, initial_used):
    import time
    from types import SimpleNamespace
    from strategies.grid.execution.request_budget import RequestBudget,request_weight
    from strategies.grid.execution.exchange import ClassicGridExchange
    from strategies.grid.execution.main import ExecutorService
    from strategies.grid.execution.risk import RiskAssessment,RiskStatus
    from strategies.grid.execution.planner import HardLimits
    from test_signal_execution_receipt import decision
    weights=[];orders={};waits=[]
    async def sleep(delay): waits.append(delay);raise AssertionError('batch waited after admission')
    budget=RequestBudget(sleep=sleep)
    if initial_used:budget.used.append((budget.clock(), initial_used))
    async def request(method,path,params=None,**kwargs):
        w=request_weight(method,path,params);await budget.acquire(w);weights.append(w)
        if path.endswith('/dual'):return {'dualSidePosition':False}
        if path.endswith('/symbolConfig'):return [dict(symbol='ETHUSDT',marginType='crossed',leverage=5)]
        if path.endswith('/accountConfig'):return dict(canTrade=True,multiAssetsMargin=False,dualSidePosition=False)
        if path.endswith('/account'):return dict(totalMarginBalance='286',availableBalance='286')
        if path.endswith('/positionRisk'):return []
        if path.endswith('/openOrders'):return list(orders.values())
        if path.endswith('/openAlgoOrders'):return []
        if path.endswith('/price') or path.endswith('/premiumIndex'):return dict(symbol='ETHUSDT',price='1699.70',markPrice='1699.70')
        if path.endswith('/exchangeInfo'):
            return {'symbols':[dict(symbol='ETHUSDT',status='TRADING',filters=[
                dict(filterType='PRICE_FILTER',tickSize='.01'),dict(filterType='LOT_SIZE',stepSize='.001',minQty='.001'),
                dict(filterType='MIN_NOTIONAL',notional='20')])]}
        if path.endswith('/order') and method=='POST':
            cid=params['newClientOrderId'];row=dict(symbol='ETHUSDT',clientOrderId=cid,orderId=len(orders)+1,
                side=params['side'],price=params['price'],origQty=params['quantity'],executedQty='0',status='NEW')
            orders[cid]=row;return row
        raise AssertionError((method,path))
    client=SimpleNamespace(use_unified_account=False,_request=request,_request_once=request,request_budget=budget)
    x=ClassicGridExchange(client)
    path=tmp_path/'batch';migrate(path);store=DecisionStore(path)
    s=ExecutorService.__new__(ExecutorService);s.exchange=x
    s.settings=SimpleNamespace(max_margin_usdt=Decimal(240),max_notional_usdt=Decimal(2400),max_open_orders=13,expected_leverage=5)
    d=decision();now=int(time.time()*1000);d.update(calculated_at_ms=now,margin_used='106',sizing_version=2,
        execution_constraints={'min_grid_count':4},last_closed_kline_ms={tf:now-60000 for tf in ('15m','1h','4h')})
    d.pop('decision_id');d['decision_id']=store.publish(d,'batch')
    assert x.can_start_batch(11) == (initial_used == 0)
    plan=await s._plan(d);engine=GridEngine(store,x,HardLimits(Decimal(240),Decimal(2400),13,5))
    started=await engine.apply_decision(d,plan,RiskAssessment(RiskStatus.ALLOW,()),leverage=5,now_ms=now,
        stop_loss_low=plan.stop_loss_low,stop_loss_high=plan.stop_loss_high)
    if initial_used:
        assert not started and not orders and not waits
        assert store.active_epoch_id() is None and store.next_decision()['decision_id']==d['decision_id']
        store.close();return
    assert started
    assert store.engine_state()['state']=='ACTIVE' and len(orders)==11 and not waits
    assert sum(weights) < 960
    before=len(weights)
    for r in orders.values():
        await engine.on_order_update({'e':'ORDER_TRADE_UPDATE','o':dict(s='ETHUSDT',c=r['clientOrderId'],
            i=r['orderId'],S=r['side'],x='NEW',X='NEW',l='0',z='0',L='0',t=0)},epoch_id=store.active_epoch_id(),
            levels=plan.levels,filters=s.filters,stop_loss_low=plan.stop_loss_low,stop_loss_high=plan.stop_loss_high)
    assert len(weights)==before
    store.close()
