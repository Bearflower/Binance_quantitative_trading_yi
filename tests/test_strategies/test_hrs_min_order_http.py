"""Real PostgreSQL plus the real final Binance transport authorization hook."""
from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from shared.binance_api import BinanceClient
from shared.entry_risk_policy import EntryDenied, RiskPolicyStore, entry_allowed
from strategies.hrs.executor import TradingExecutor
from tests.test_risk.test_hrs_scoped_slots import runtime, pytestmark
from tests.test_risk.test_hrs_min_order_lock import enable, plan, proposal


@pytest.mark.asyncio
async def test_http_rechecks_original_risk_after_preleverage_authorization(runtime,monkeypatch):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    client=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    db=SimpleNamespace(pool=r.pool)
    assert await entry_allowed(SimpleNamespace(db=db),'hrs',risk_decision=decision,quantity=decision.quantity)
    await r.admin.execute('UPDATE trading.entry_risk_reservation SET original_pressure_risk=0 WHERE client_order_id=$1',client)
    transport=AsyncMock(return_value={'sent':True})
    executor=SimpleNamespace(db=db,binance_api=SimpleNamespace(place_order=transport),order_type_entry='LIMIT')
    with pytest.raises(EntryDenied):
        await TradingExecutor._submit_reserved_hrs_entry(executor,'A','BUY',decision.quantity,sized.price,decision)
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_http_authorization_rechecks_after_rate_limiter_on_same_locked_connection(runtime,monkeypatch):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    client_id=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client_id)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    client=BinanceClient('test_api_key_12345','test_api_secret_67890')
    client.use_unified_account=False;client._init_session=AsyncMock();client.session=MagicMock()
    client.get_symbol_info=AsyncMock(return_value={'stepSize':'.1','tickSize':D('.01')})
    client.session.request.side_effect=AssertionError('transport must not run')
    async def rate_wait():
        await r.admin.execute('UPDATE trading.entry_risk_reservation SET original_pressure_risk=0 WHERE client_order_id=$1',client_id)
    client.rate_limiter.acquire=AsyncMock(side_effect=rate_wait)
    connections=[]
    authorize=RiskPolicyStore.authorize_hrs_floor
    async def observed(store,connection,*args,**kwargs):
        connections.append(id(connection))
        return await authorize(store,connection,*args,**kwargs)
    monkeypatch.setattr(RiskPolicyStore,'authorize_hrs_floor',observed)
    executor=SimpleNamespace(db=SimpleNamespace(pool=r.pool),binance_api=client,order_type_entry='LIMIT')
    with pytest.raises(EntryDenied):
        await TradingExecutor._submit_reserved_hrs_entry(executor,'A','BUY',decision.quantity,sized.price,decision)
    client.session.request.assert_not_called()
    client.rate_limiter.acquire.assert_awaited_once()
    assert len(connections)==2 and len(set(connections))==1
    assert await r.admin.fetchval('SELECT state FROM trading.entry_risk_reservation WHERE client_order_id=$1',client_id)=='released'


@pytest.mark.asyncio
async def test_valid_floor_intent_reaches_real_post_boundary(runtime,monkeypatch):
    r=runtime;snapshot=await enable(r);sized,geometry=proposal(plan())
    client_id=await r.store.reserve('hrs','A','a',sized,2,snapshot,geometry=geometry)
    decision=replace(r.store.reservation_audit['risk_decision'],client_order_id=client_id)
    monkeypatch.setenv('TRADE_ACCOUNT_ID','test')
    client=BinanceClient('test_api_key_12345','test_api_secret_67890')
    client.use_unified_account=False;client._init_session=AsyncMock();client.session=MagicMock()
    client.get_symbol_info=AsyncMock(return_value={'stepSize':'.1','tickSize':D('.01')})
    client.rate_limiter.acquire=AsyncMock()
    class ReachedTransport(RuntimeError):pass
    client.session.request.side_effect=ReachedTransport('POST boundary reached')
    executor=SimpleNamespace(db=SimpleNamespace(pool=r.pool),binance_api=client,order_type_entry='LIMIT')
    with pytest.raises(ReachedTransport,match='POST boundary reached'):
        await TradingExecutor._submit_reserved_hrs_entry(executor,'A','BUY',decision.quantity,sized.price,decision)
    assert client.session.request.call_count==1
    args=client.session.request.call_args
    assert args.args[0]=='POST'
    assert D(args.kwargs['params']['quantity'])==decision.quantity
    assert args.kwargs['params']['newClientOrderId']==client_id
