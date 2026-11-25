import asyncio
import runpy
from decimal import Decimal as D
from unittest.mock import AsyncMock
import pytest

from shared.binance_api import BinanceAPIError
from strategies.grid.execution.exchange import GridExchange, StopCancellationUncertain
from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.store import DecisionStore,migrate
from strategies.grid.execution.risk import RiskLimits,RiskStatus
from strategies.grid.execution.risk_tracker import RiskTracker


@pytest.mark.asyncio
async def test_expired_stop_finishes_exit_without_repeated_cancel(tmp_path):
    path=tmp_path/"db";migrate(path);store=DecisionStore(path)
    try:
        epoch=store.create_epoch("s")
        store.conn.execute("insert into protection values('eg-stop',?,'BUY','2100','UNKNOWN',NULL)",(epoch,))
        store.set_engine_state("LOCKED","stop cancellation uncertain")
        exchange=AsyncMock()
        exchange.query_stop.return_value={'clientAlgoId':'eg-stop','algoStatus':'EXPIRED','actualOrderId':'','algoId':9}
        exchange.position.return_value=[{'positionSide':'BOTH','positionAmt':'0'}]
        exchange.open_orders.return_value=exchange.open_stops.return_value=[]
        engine=GridEngine(store,exchange)
        assert await engine.exit_all(epoch)
        assert store.active_epoch_id() is None
        assert store.active_protections()==[]
        exchange.cancel_stop.assert_not_awaited()
        assert store.conn.execute("select exchange_algo_id from protection").fetchone()[0]=='9'
    finally:store.close()


@pytest.mark.asyncio
async def test_query_stop_waits_for_visibility_without_resending(monkeypatch):
    monkeypatch.setattr(asyncio,"sleep",AsyncMock())
    client=AsyncMock();client.use_unified_account=True
    client._request.side_effect=[BinanceAPIError(-2013,"missing"),{'algoStatus':'NEW'}]
    assert (await GridExchange(client).query_stop('eg-stop'))['algoStatus']=='NEW'
    assert all(c.args[0]=='GET' for c in client._request.call_args_list)


@pytest.mark.asyncio
async def test_expired_stop_cancel_is_idempotent():
    client=AsyncMock();client.use_unified_account=True
    client._request.return_value={'clientAlgoId':'eg-stop','algoStatus':'EXPIRED','actualOrderId':''}
    assert (await GridExchange(client).cancel_stop('eg-stop'))['complete']
    assert client._request.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize("amount,expected",[("0.05956175",True),("50",False)])
async def test_internal_transfer_requires_matching_counterparty(amount,expected):
    client=AsyncMock();client.use_unified_account=True
    wallet=AsyncMock()
    wallet._request.return_value={'rows':[{'txId':1,'asset':'USDT','amount':'0.05956175',
        'status':'CONFIRMED','type':'ROLL_OUT','transFrom':'CROSS_MARGIN',
        'transTo':'FUTURES','timestamp':1800000000000}]}
    evidence=await GridExchange(client,wallet).confirm_internal_settlement({
        'tranId':1,'asset':'USDT','income':amount,'symbol':'','time':1800000000201})
    assert bool(evidence)==expected


@pytest.mark.asyncio
async def test_verified_internal_transfer_is_durable_and_external_transfer_locks(tmp_path):
    path=tmp_path/"db";migrate(path);store=DecisionStore(path)
    try:
        exchange=runpy.run_path("tests/test_grid_execution/test_risk_tracker.py")["FakeRiskExchange"]("300")
        now=1800000000000
        tracker=RiskTracker(store,exchange,RiskLimits(D(15),D(30)))
        assert (await tracker.assess(now)).status==RiskStatus.ALLOW
        exchange.income_rows=[{'incomeType':'TRANSFER','income':'0.05956175','tranId':1,'asset':'USDT','time':now}]
        exchange.confirm_internal_settlement=AsyncMock(return_value={'status':'CONFIRMED','txId':1})
        assert (await tracker.assess(now)).status==RiskStatus.ALLOW
        exchange.confirm_internal_settlement.reset_mock()
        assert (await tracker.assess(now)).status==RiskStatus.ALLOW
        exchange.confirm_internal_settlement.assert_not_awaited()
        exchange.income_rows[0]=dict(exchange.income_rows[0],tranId=2,income='50')
        exchange.confirm_internal_settlement.return_value=None
        assert (await tracker.assess(now)).status==RiskStatus.UNKNOWN_LOCKED
    finally:store.close()


@pytest.mark.asyncio
async def test_locked_cleanup_is_rate_limited_but_still_retried(tmp_path,monkeypatch):
    import time
    from unittest.mock import Mock
    from strategies.grid.execution.main import ExecutorService
    from strategies.grid.execution.risk import RiskAssessment
    path=tmp_path/"db";migrate(path);store=DecisionStore(path)
    try:
        store.create_epoch("s");store.set_engine_state("LOCKED","uncertain")
        service=ExecutorService.__new__(ExecutorService)
        service.store=store;service.guard=asyncio.Lock()
        from types import SimpleNamespace
        service.engine=SimpleNamespace(_lock=Mock(), exit_all=AsyncMock(return_value=False))
        service._risk=AsyncMock(return_value=RiskAssessment(RiskStatus.UNKNOWN_LOCKED,("account_transfer",)))
        service._write_heartbeat=Mock()
        clock=[1000]
        monkeypatch.setattr(time,"monotonic",lambda:clock[0])
        await service.tick();await service.tick()
        service.engine.exit_all.assert_awaited_once()
        clock[0]=1061
        await service.tick()
        assert service.engine.exit_all.await_count==2
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("final_status, succeeds",[("EXPIRED",True),("NEW",False)])
async def test_cancel_unknown_requires_terminal_query_confirmation(final_status,succeeds):
    client=AsyncMock();client.use_unified_account=True
    client._request_once=client._request
    client._request.side_effect=[
        {'clientAlgoId':'eg-stop','algoStatus':'NEW'},
        BinanceAPIError(-2011,'unknown'),
        *[{'clientAlgoId':'eg-stop','algoStatus':final_status,'actualOrderId':''}
          for _ in range(1 if succeeds else 4)]]
    exchange=GridExchange(client)
    if succeeds:
        assert (await exchange.cancel_stop('eg-stop'))['complete']
    else:
        with pytest.raises(StopCancellationUncertain, match="algoStatus.*NEW"):
            await exchange.cancel_stop('eg-stop')
    assert client._request.await_count==(3 if succeeds else 6)


@pytest.mark.asyncio
async def test_stop_visibility_retry_has_a_finite_bound(monkeypatch):
    monkeypatch.setattr(asyncio,"sleep",AsyncMock())
    client=AsyncMock();client.use_unified_account=True
    client._request.side_effect=BinanceAPIError(-2013,'missing')
    with pytest.raises(BinanceAPIError):
        await GridExchange(client).query_stop('eg-stop')
    assert client._request.await_count==4


@pytest.mark.asyncio
async def test_flat_round_trip_retires_expired_stop_without_ending_active_grid(tmp_path):
    from strategies.grid.execution.fills import ProtectionCoordinator
    path=tmp_path/"db";migrate(path);store=DecisionStore(path)
    try:
        epoch=store.create_epoch("s")
        store.conn.execute("insert into protection values('eg-stop',?,'BUY','2100','NEW','9')",(epoch,))
        store.set_engine_state("ACTIVE")
        exchange=AsyncMock()
        exchange.position.return_value=[{'positionSide':'BOTH','positionAmt':'0'}]
        exchange.open_stops.return_value=[]
        exchange.query_stop.return_value={'clientAlgoId':'eg-stop','algoStatus':'EXPIRED','actualOrderId':'','algoId':9}
        assert await ProtectionCoordinator(store,exchange).ensure(epoch,D(0),D(1800),D(2100))
        assert store.engine_state()['state']=='ACTIVE'
        assert store.active_protections()==[]
        exchange.cancel_stop.assert_not_awaited()
        store.conn.execute("update protection set status='NEW'")
        await GridEngine(store,exchange).sync_triggered_stops()
        assert store.engine_state()['state']=='ACTIVE'
        assert store.active_epoch_id()==epoch
    finally:store.close()
