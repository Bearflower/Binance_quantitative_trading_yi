import asyncio
import json
import time
from decimal import Decimal as D
from unittest.mock import AsyncMock, Mock

import pytest

from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.fills import FillCoordinator
from strategies.grid.execution.main import ExecutorService
from strategies.grid.execution.planner import GridPlan, PlannedOrder, ExchangeFilters
from strategies.grid.execution.risk import RiskAssessment, RiskStatus
from strategies.grid.execution.store import DecisionStore, migrate


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'review.sqlite'
    migrate(path)
    result = DecisionStore(path)
    yield result
    result.close()


def event(client='eg-a', order=1, trade=1, quantity='.02', cumulative='.02'):
    return {'e': 'ORDER_TRADE_UPDATE', 'o': {
        's': 'ETHUSDT', 'c': client, 'i': order, 'S': 'BUY', 'x': 'TRADE',
        'X': 'FILLED', 'l': quantity, 'z': cumulative, 'L': '1900', 't': trade}}


def entry(store, epoch, client='eg-a', role='ENTRY', level=0, quantity='.02'):
    store.prepare_order(epoch, level, role, 'BUY', D(1900), D(quantity), client)
    store.record_order(client, '1', 'NEW', D(0))


def test_canceled_reverse_releases_unexecuted_allocation(store):
    epoch = store.create_epoch('d')
    entry(store, epoch, quantity='.04')
    fc = FillCoordinator(store)
    args = ('eg-a', (D(1900), D(1950)), D('.001'), D('.001'), D(5))
    fc.record(event(cumulative='.02'))
    first = fc.prepare_reverse(*args)
    store.record_order(first.client_order_id, '2', 'CANCELED', D(0))
    fc.record(event(trade=2, cumulative='.04'))
    second = fc.prepare_reverse(*args)
    assert second.quantity == D('.04')


def test_duplicate_earlier_partial_trade_is_idempotent(store):
    epoch = store.create_epoch('d')
    entry(store, epoch, quantity='.04')
    fc = FillCoordinator(store)
    fc.record(event(cumulative='.02'))
    fc.record(event(trade=2, cumulative='.04'))
    assert fc.record(event(cumulative='.02')) is False
    assert D(store.get_order('eg-a')['executed_qty']) == D('.04')


@pytest.mark.asyncio
async def test_old_exit_cannot_create_reverse_in_new_epoch(store):
    old = store.create_epoch('old')
    entry(store, old, role='EXIT', level=-1)
    FillCoordinator(store).record(event())
    store.close_epoch(old)
    current = store.create_epoch('current')
    store.set_engine_state('ACTIVE')
    exchange = Mock()
    exchange.place_limit = AsyncMock()
    assert await GridEngine(store, exchange).on_order_update(
        event(), epoch_id=current, levels=(D(1800), D(1900)),
        filters=ExchangeFilters(D('.01'), D('.001'), D('.001'), D(5)),
        stop_loss_low=D(1700), stop_loss_high=D(2100))
    exchange.place_limit.assert_not_awaited()


@pytest.mark.asyncio
async def test_locked_fill_is_recorded_and_cleanup_attempted(store):
    epoch = store.create_epoch('d')
    entry(store, epoch)
    store.set_engine_state('LOCKED', 'uncertain submit')
    engine = GridEngine(store, Mock())
    engine.exit_all = AsyncMock(return_value=False)
    assert not await engine.on_order_update(event(), epoch_id=epoch, levels=(),
        filters=None, stop_loss_low=D(1800), stop_loss_high=D(2100))
    assert len(store.fills('eg-a')) == 1
    engine.exit_all.assert_awaited_once_with(epoch)


@pytest.mark.asyncio
async def test_stale_consumed_signal_pauses_active_epoch(store):
    now = int(time.time()*1000)
    decision = store.publish({'symbol':'ETHUSDT','state':'OSCILLATION',
        'calculated_at_ms':now-86400000,'executable':True,
        'last_closed_kline_ms':{'15m':now-86400001,'1h':now-86400001,'4h':now-86400001}}, 'hash')
    epoch=store.create_epoch(decision, stop_loss_low=D(1800), stop_loss_high=D(2100))
    store.acknowledge(decision)
    store.set_engine_state('ACTIVE')
    service=ExecutorService.__new__(ExecutorService)
    service.store=store
    service.guard=asyncio.Lock()
    service.engine=Mock(pause=AsyncMock(return_value=True), enforce_limits=AsyncMock(return_value=True))
    service._risk=AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW, ()))
    service._write_heartbeat=Mock()
    await service.tick()
    service.engine.pause.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_child_rest_backfill_is_durable_and_idempotent(store):
    epoch=store.create_epoch('d')
    store.conn.execute('INSERT INTO protection VALUES(?,?,?,?,?,?)',
        ('eg-s-a',epoch,'SELL','1800','NEW','9'))
    store.set_engine_state('ACTIVE')
    exchange=Mock()
    exchange.query_stop=AsyncMock(return_value={'clientAlgoId':'eg-s-a','algoStatus':'FINISHED','actualOrderId':55})
    exchange.query_order_by_id=AsyncMock(return_value={'symbol':'ETHUSDT','side':'SELL',
        'orderId':55,'origQty':'.02','executedQty':'.02','status':'FILLED'})
    exchange.order_trades=AsyncMock(return_value=[{'symbol':'ETHUSDT','side':'SELL','orderId':55,'id':66,'qty':'.02','price':'1800'}])
    engine=GridEngine(store, exchange)
    await engine.sync_triggered_stops()
    await engine.sync_triggered_stops()
    assert store.get_order('eg-stop-55')['role']=='STOP'
    assert len(store.fills('eg-stop-55'))==1
    assert store.engine_state()['state']=='EXITING'
    assert store.active_protections()==[]


@pytest.mark.asyncio
async def test_alert_retry_after_failed_delivery(store, monkeypatch):
    store.set_engine_state('LOCKED','risk')
    svc=ExecutorService.__new__(ExecutorService)
    svc.store=store
    svc.alert_sender=AsyncMock(side_effect=[False,True])
    svc._last_alert_token=None
    now=[1000.]
    monkeypatch.setattr('strategies.grid.execution.main.time.monotonic',lambda:now[0])
    await svc._alert_if_needed()
    now[0]+=31
    await svc._alert_if_needed()
    now[0]+=60
    await svc._alert_if_needed()
    assert svc.alert_sender.await_count==2


@pytest.mark.asyncio
async def test_second_submit_timeout_cancels_first_accepted_entry(store):
    import runpy
    fixture=runpy.run_path('tests/test_grid_execution/test_start.py')
    exchange=fixture['StartExchange']()
    place=exchange.place_limit
    async def submit(*args):
        if exchange.sent:
            raise TimeoutError('lost second response')
        return await place(*args)
    async def cancel(client_id):
        row=next((o for o in exchange.ordinary if o['clientOrderId']==client_id),None)
        if row is None:
            raise RuntimeError('uncertain order')
        exchange.ordinary.remove(row)
        return dict(row,status='CANCELED')
    exchange.place_limit=submit
    exchange.cancel_order=cancel
    now=int(time.time()*1000)
    decision=fixture['_decision'](store,now)
    plan=GridPlan(tuple(PlannedOrder(i,'BUY',D(p),D('.02')) for i,p in enumerate(('1900','1950'))),
                  (D(1900),D(1950),D(2000)),D(77),D('7.7'))
    assert not await GridEngine(store,exchange).start_grid(decision,plan,
        RiskAssessment(RiskStatus.ALLOW,()),leverage=10,now_ms=now)
    assert exchange.ordinary==[]
    assert store.engine_state()['state']=='LOCKED'


@pytest.mark.asyncio
async def test_wrong_side_exchange_stop_cannot_protect_long(store):
    epoch=store.create_epoch('d')
    entry(store,epoch)
    FillCoordinator(store).record(event())
    store.conn.execute('INSERT INTO protection VALUES(?,?,?,?,?,?)',
        ('eg-s-wrong',epoch,'BUY','2100','NEW','9'))
    exchange=Mock()
    exchange.query_stop=AsyncMock(return_value={'algoStatus':'NEW'})
    exchange.open_orders=AsyncMock(return_value=[])
    exchange.open_stops=AsyncMock(return_value=[{'symbol':'ETHUSDT','clientAlgoId':'eg-s-wrong',
        'side':'BUY','algoStatus':'NEW','closePosition':True,'workingType':'MARK_PRICE',
        'orderType':'STOP_MARKET','positionSide':'BOTH','triggerPrice':'2100'}])
    exchange.position=AsyncMock(return_value=[{'positionSide':'BOTH','positionAmt':'.02'}])
    assert not await GridEngine(store,exchange).reconcile()
    assert 'valid exchange stop' in store.engine_state()['lock_reason']


@pytest.mark.asyncio
async def test_rest_known_fill_still_creates_reverse_once_on_stream_replay(store):
    import runpy
    exchange=runpy.run_path('tests/test_grid_execution/test_engine_fill.py')['FillExchange']()
    epoch=store.create_epoch('d')
    entry(store,epoch)
    FillCoordinator(store).record(event())
    store.set_engine_state('ACTIVE')
    engine=GridEngine(store,exchange)
    kwargs=dict(epoch_id=epoch,levels=(D(1900),D(1950)),
        filters=ExchangeFilters(D('.01'),D('.001'),D('.001'),D(5)),
        stop_loss_low=D(1800),stop_loss_high=D(2100))
    assert await engine.on_order_update(event(),**kwargs)
    assert await engine.on_order_update(event(),**kwargs)
    assert len(exchange.ordinary)==1


@pytest.mark.asyncio
async def test_another_sources_reverse_waits_then_drains_without_cancellation(store):
    epoch=store.create_epoch('d')
    for client,oid,tid in [('eg-a','1','1'),('eg-b','2','2')]:
        entry(store,epoch,client=client)
        store.record_order(client,oid,'FILLED',D('.02'))
        store.record_fill(client,oid,tid,D('.02'),D(1900))
    store.set_engine_state('ACTIVE')
    exchange=Mock()
    exchange.cancel_order=AsyncMock()
    exchange.place_limit=AsyncMock(return_value={'status':'NEW','orderId':'3','executedQty':'0'})
    engine=GridEngine(store,exchange)
    filters=ExchangeFilters(D('.01'),D('.001'),D('.001'),D(5))
    levels=(D(1900),D(1950))
    assert await engine._replenish('eg-a',levels,filters)
    assert await engine._replenish('eg-b',levels,filters)
    exchange.cancel_order.assert_not_awaited()
    assert exchange.place_limit.await_count==1
    reverse=next(o for o in store.active_orders() if o['role']=='REVERSE')
    store.record_order(reverse['client_order_id'],'3','FILLED',D('.02'))
    store.record_fill(reverse['client_order_id'],'3','3',D('.02'),D(1950))
    assert await engine._replenish('eg-b',levels,filters)
    assert exchange.place_limit.await_count==2


@pytest.mark.asyncio
async def test_stop_discovered_during_decision_cannot_restart_from_same_signal(store):
    import runpy
    now=int(time.time()*1000)
    decision=runpy.run_path('tests/test_grid_execution/test_start.py')['_decision'](store,now)
    epoch=store.create_epoch('active')
    store.set_engine_state('ACTIVE')
    engine=GridEngine(store,Mock())
    async def discover_stop():
        store.set_engine_state('EXITING')
        return True
    engine.reconcile=AsyncMock(side_effect=discover_stop)
    engine.exit_all=AsyncMock(return_value=True)
    engine.start_grid=AsyncMock()
    assert await engine.apply_decision(decision,None,RiskAssessment(RiskStatus.ALLOW,()),
        leverage=5,now_ms=now,stop_loss_low=D(1800),stop_loss_high=D(2100))
    engine.exit_all.assert_awaited_once_with(epoch)
    engine.start_grid.assert_not_awaited()
    assert store.cursor()==decision['decision_id']


@pytest.mark.asyncio
async def test_tick_recovers_rest_only_fill_without_any_stream_event(store):
    import runpy
    exchange=runpy.run_path('tests/test_grid_execution/test_engine_fill.py')['FillExchange']()
    now=int(time.time()*1000)
    decision=runpy.run_path('tests/test_grid_execution/test_start.py')['_decision'](store,now)
    epoch=store.create_epoch(decision['decision_id'],levels=(D(1900),D(1950)),
        stop_loss_low=D(1800),stop_loss_high=D(2100))
    store.acknowledge(decision['decision_id'])
    entry(store,epoch)
    FillCoordinator(store).record(event())
    store.set_engine_state('ACTIVE')
    from strategies.grid.execution.fills import ProtectionCoordinator
    assert await ProtectionCoordinator(store,exchange).ensure(epoch,D('.02'),D(1800),D(2100))
    svc=ExecutorService.__new__(ExecutorService)
    svc.store=store
    svc.guard=asyncio.Lock()
    svc.engine=GridEngine(store,exchange)
    svc.stream=Mock(ready=True)
    svc.filters=ExchangeFilters(D('.01'),D('.001'),D('.001'),D(5))
    svc._risk=AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW,()))
    svc._write_heartbeat=Mock()
    await svc.tick()
    await svc.tick()
    assert len(exchange.ordinary)==1


@pytest.mark.asyncio
async def test_stop_transaction_rolls_back_and_recovers_after_restart(store, tmp_path):
    import sqlite3
    import runpy
    now = int(time.time() * 1000)
    publish = runpy.run_path('tests/test_grid_execution/test_start.py')['_decision']
    first = publish(store, now)
    store.acknowledge(first['decision_id'])
    second = publish(store, now + 1000)
    epoch = store.create_epoch('active')
    store.set_engine_state('ACTIVE')
    store.conn.execute("INSERT INTO protection VALUES('s',?,'SELL','1800','NEW','9')", (epoch,))
    store.conn.execute("""CREATE TEMP TRIGGER abort_exit BEFORE UPDATE ON engine_state
        BEGIN SELECT RAISE(ABORT,'simulated failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.settle_stop(epoch, 's')
    assert store.active_protections()[0]['status'] == 'NEW'
    assert not store.stopped_epoch(epoch)
    assert store.cursor() == first['decision_id']
    assert store.next_decision()['decision_id'] == second['decision_id']
    # Emulate an old-version crash between the original independent commits.
    store.record_protection('s', 'TRIGGERED', '9')
    recovered = DecisionStore(tmp_path / 'review.sqlite')
    try:
        await GridEngine(recovered, Mock()).sync_triggered_stops()
        assert recovered.engine_state()['state'] == 'EXITING'
        assert recovered.stopped_epoch(epoch)
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_ws_stop_exits_and_rejects_delayed_pre_stop_signal(store, tmp_path, monkeypatch):
    import runpy
    now = 1_800_000_000_000
    monkeypatch.setattr(time, 'time', lambda: now / 1000)
    old = runpy.run_path('tests/test_grid_execution/test_start.py')['_decision'](store, now)
    epoch = store.create_epoch('active')
    store.set_engine_state('ACTIVE')
    entry(store, epoch, client='eg-stop-1', role='STOP')
    exchange = AsyncMock()
    exchange.position.return_value = [{'positionSide': 'BOTH', 'positionAmt': '0'}]
    exchange.open_orders.return_value = exchange.open_stops.return_value = []
    # The closing BUY offsets an existing SELL.
    store.prepare_order(epoch, 1, 'ENTRY', 'SELL', D(1900), D('.02'), 'eg-sell')
    store.record_order('eg-sell', '2', 'FILLED', D('.02'))
    store.record_fill('eg-sell', '2', '2', D('.02'), D(1900))
    engine = GridEngine(store, exchange)
    assert await engine.on_order_update(event(client='eg-stop-1'), epoch_id=epoch,
        levels=(), filters=None, stop_loss_low=D(1800), stop_loss_high=D(2100))
    assert store.engine_state()['state'] == 'FLAT'
    assert store.cursor() == old['decision_id']
    reopened = DecisionStore(tmp_path / 'review.sqlite')
    try:
        payload = {k: v for k, v in old.items() if k not in ('decision_id', 'config_hash')}
        reopened.publish(dict(payload, calculated_at_ms=now), 'late')
        assert reopened.next_decision() is None
        new = reopened.publish(dict(payload, calculated_at_ms=now + 1), 'fresh')
        assert reopened.next_decision()['decision_id'] == new
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_rest_exit_keeps_signal_published_after_stop(store, monkeypatch):
    import runpy
    now = 1_800_000_000_000
    monkeypatch.setattr(time, 'time', lambda: now / 1000)
    publish = runpy.run_path('tests/test_grid_execution/test_start.py')['_decision']
    publish(store, now)
    epoch = store.create_epoch('active')
    store.set_engine_state('ACTIVE')
    store.conn.execute("INSERT INTO protection VALUES('s',?,'SELL','1800','NEW','9')", (epoch,))
    store.settle_stop(epoch, 's')
    fresh = publish(store, now + 2000)
    exchange = AsyncMock()
    exchange.position.return_value = [{'positionSide': 'BOTH', 'positionAmt': '0'}]
    exchange.open_orders.return_value = exchange.open_stops.return_value = []
    service = ExecutorService.__new__(ExecutorService)
    service.engine = GridEngine(store, exchange)
    service.store = store
    service.guard = asyncio.Lock()
    service._risk = AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW, ()))
    service._write_heartbeat = Mock()
    await service.tick()
    assert store.active_epoch_id() is None
    assert store.engine_state()['state'] == 'FLAT'
    assert store.next_decision()['decision_id'] == fresh['decision_id']



@pytest.mark.asyncio
async def test_fresh_signal_starts_grid_after_stop_exit_and_restart(store, tmp_path, monkeypatch):
    import runpy
    now = 1_800_000_000_000
    monkeypatch.setattr(time, 'time', lambda: now / 1000)
    helpers = runpy.run_path('tests/test_grid_execution/test_start.py')
    helpers['_decision'](store, now)
    epoch = store.create_epoch('old-grid')
    store.set_engine_state('ACTIVE')
    store.settle_stop(epoch)
    exchange = helpers['StartExchange']()
    assert await GridEngine(store, exchange).exit_all(epoch)
    reopened = DecisionStore(tmp_path / 'review.sqlite')
    try:
        fresh = helpers['_decision'](reopened, now + 2000)
        plan = GridPlan(
            (PlannedOrder(0, 'BUY', D(1900), D('.02')),),
            (D(1900), D(1950)), D(38), D('3.8'), D(1800), D(2100))
        assert await GridEngine(reopened, exchange).apply_decision(
            fresh, plan, RiskAssessment(RiskStatus.ALLOW, ()),
            leverage=10, now_ms=now + 2000,
            stop_loss_low=D(1800), stop_loss_high=D(2100))
        assert reopened.engine_state()['state'] == 'ACTIVE'
        assert reopened.active_epoch_id() != epoch
        assert reopened.cursor() == fresh['decision_id']
        assert len(exchange.sent) == 1
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('position,side,expected', [
    ('.013', 'SELL', '1553.20'), ('-.013', 'BUY', '1846.18'),
])
async def test_nonpassive_start_uses_quantized_plan_stop_for_remaining_position(store, position, side, expected):
    """启动成交且立即退出失败时，保护实际余仓也必须使用已量化的计划价格。"""
    now = 1_800_000_000_000
    payload = dict(symbol='ETHUSDT', state='OSCILLATION', calculated_at_ms=now-1000,
        last_closed_kline_ms={key: now-60000 for key in ('15m', '1h', '4h')},
        executable=True, feasibility={'feasible': True},
        grid_params={'stop_loss_low': '1553.20347', 'stop_loss_high': '1846.18947'})
    store.publish(payload, 'quantized-stop')
    decision = store.next_decision()
    plan = GridPlan((PlannedOrder(0, 'BUY' if side == 'SELL' else 'SELL', D(1600), D('.013')),),
                   (D(1600), D(1800)), D(21), D('4.2'), D('1553.20'), D('1846.18'))
    exchange = AsyncMock()
    exchange.verify_account.return_value = ExchangeFilters(D('.01'), D('.001'), D('.001'), D(20))
    exchange.account_info.return_value = {'totalAvailableBalance': '100'}
    exchange.place_limit.return_value = {'orderId': 1, 'status': 'FILLED', 'executedQty': '.013'}
    exchange.position.return_value = [{'symbol': 'ETHUSDT', 'positionSide': 'BOTH', 'positionAmt': position}]
    exchange.open_stops.return_value = []
    accepted = {}
    async def place_stop(stop_side, trigger, client_id):
        if trigger % D('.01'):
            raise ValueError('交易所拒绝未量化的触发价')
        accepted.update(clientAlgoId=client_id, algoId=2, side=stop_side,
                        triggerPrice=str(trigger), algoStatus='NEW')
        return accepted.copy()
    exchange.place_stop.side_effect = place_stop
    exchange.query_stop.side_effect = lambda client_id: accepted.copy()
    engine = GridEngine(store, exchange)
    engine.reconcile = AsyncMock(return_value=True)
    engine.check_limits = AsyncMock()
    engine.exit_all = AsyncMock(return_value=False)
    assert not await engine.start_grid(decision, plan, RiskAssessment(RiskStatus.ALLOW, ()),
                                       leverage=10, now_ms=now)
    engine.exit_all.assert_awaited_once()
    assert exchange.place_stop.await_args.args[:2] == (side, D(expected))
    exchange.query_stop.assert_awaited_once()
    assert store.active_protections()[0]['status'] == 'NEW'
    assert store.engine_state()['state'] == 'LOCKED'
