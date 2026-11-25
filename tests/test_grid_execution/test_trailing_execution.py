import asyncio
import json
import time
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from shared.binance_api import BinanceAPIError
from strategies.grid.execution.engine import GridEngine
from strategies.grid.execution.fills import FillCoordinator
from strategies.grid.execution.planner import ExchangeFilters, HardLimits
from strategies.grid.execution.store import DecisionStore, migrate
from strategies.grid.execution.trailing import TrailingController


class MovingExchange:
    def __init__(self):
        self.mark = D(150)
        self.orders = {}
        self.stops = {}
        self.trades = []
        self.quantity = D(0)
        self.cancel_calls = []
        self.place_calls = []
        self.close_position = AsyncMock()

    async def position(self):
        return [{'symbol': 'ETHUSDT', 'positionSide': 'BOTH', 'positionAmt': str(self.quantity)}]

    async def mark_price(self): return self.mark
    async def ticker_price(self): return self.mark
    async def account_info(self): return {'totalAvailableBalance': '300'}
    async def verify_account(self, leverage):
        assert leverage == 5
        return ExchangeFilters(D('.01'), D('.001'), D('.001'), D(5))
    async def open_orders(self): return [dict(o) for o in self.orders.values() if o['status'] in ('NEW', 'PARTIALLY_FILLED')]
    async def open_stops(self): return [dict(s) for s in self.stops.values() if s['algoStatus'] == 'NEW']
    async def place_stop(self, side, trigger, client):
        row = {'symbol': 'ETHUSDT', 'clientAlgoId': client, 'algoId': 100 + len(self.stops),
               'side': side, 'triggerPrice': str(trigger), 'algoStatus': 'NEW', 'closePosition': True,
               'workingType': 'MARK_PRICE', 'orderType': 'STOP_MARKET', 'positionSide': 'BOTH'}
        self.stops[client] = row
        return dict(row)
    async def query_stop(self, client): return dict(self.stops[client])
    async def cancel_stop(self, client):
        self.stops[client]['algoStatus'] = 'CANCELED'
        return {'complete': True}
    async def order_trades(self, order_id, start, end): return [t for t in self.trades if t['orderId'] == order_id]

    async def query_order(self, client):
        if client not in self.orders:
            raise BinanceAPIError(-2013, 'missing')
        return dict(self.orders[client])

    async def cancel_order(self, client):
        self.cancel_calls.append(client)
        self.orders[client]['status'] = 'CANCELED'
        return dict(self.orders[client])

    async def place_limit(self, side, quantity, price, client):
        assert (side == 'BUY' and price < self.mark) or (side == 'SELL' and price > self.mark)
        assert client not in self.orders
        self.place_calls.append(client)
        row = {'symbol': 'ETHUSDT', 'clientOrderId': client, 'orderId': len(self.orders) + 10,
               'side': side, 'price': str(price), 'origQty': str(quantity), 'executedQty': '0', 'status': 'NEW'}
        self.orders[client] = row
        return dict(row)


@pytest.fixture
def moving(tmp_path):
    path = tmp_path / 'journal'
    migrate(path)
    store = DecisionStore(path)
    now = int(time.time() * 1000)
    payload = {'symbol': 'ETHUSDT', 'state': 'OSCILLATION', 'executable': True,
               'calculated_at_ms': now, 'last_closed_kline_ms': {tf: now - 60_000 for tf in ['15m', '1h', '4h']},
               'grid_params': {'lower_boundary': '100', 'upper_boundary': '140', 'grid_count': 4,
                               'grid_mode': 'ARITHMETIC', 'stop_loss_low': '50', 'stop_loss_high': '190',
                               'stop_move_up_price': '170', 'stop_move_down_price': '70'},
               'feasibility': {'feasible': True}}
    decision = store.publish(payload, 'hash')
    epoch = store.create_epoch(decision, levels=tuple(map(D, ['100', '110', '120', '130', '140'])),
                               stop_loss_low=D(50), stop_loss_high=D(190))
    store.acknowledge(decision)
    exchange = MovingExchange()
    for index in [0, 1]:
        client = 'eg-old-' + str(index)
        price = D(100 + index * 10)
        store.prepare_order(epoch, index, 'ENTRY', 'BUY', price, D('.1'), client)
        row = {'symbol': 'ETHUSDT', 'clientOrderId': client, 'orderId': index + 1,
               'side': 'BUY', 'price': str(price), 'origQty': '.1', 'executedQty': '0', 'status': 'NEW'}
        exchange.orders[client] = row
        store.record_order(client, str(index + 1), 'NEW', D(0))
    store.set_engine_state('ACTIVE')
    filters = ExchangeFilters(D('.01'), D('.001'), D('.001'), D(5))
    engine = GridEngine(store, exchange, HardLimits(D(240), D(2400), 13, 5))
    controller = TrailingController(engine)
    engine.trailing = controller
    controller.ensure_epoch(epoch, filters)
    yield store, exchange, engine, controller, filters, epoch
    store.close()


@pytest.mark.asyncio
async def test_move_preserves_epoch_inventory_and_pairs(moving):
    store, exchange, engine, controller, filters, epoch = moving
    assert await controller.tick(epoch, filters)
    window = controller.state.window(epoch)
    assert (window['lower_index'], window['upper_index']) == (1, 5)
    assert exchange.cancel_calls == ['eg-old-0']
    assert len(exchange.place_calls) == 1
    added = store.get_order(exchange.place_calls[0])
    assert added['level_index'] == 4 and added['side'] == 'BUY' and D(added['price']) == 140
    assert D(added['quantity']) == D('.085')
    assert store.active_epoch_id() == epoch and store.engine_state()['state'] == 'ACTIVE'
    assert not controller.state.pending(epoch)
    exchange.close_position.assert_not_awaited()
    await engine.check_limits()


@pytest.mark.asyncio
async def test_restart_after_accepted_add_queries_instead_of_resubmitting(moving, monkeypatch):
    store, exchange, engine, controller, filters, epoch = moving
    original = controller.state.finish
    def interrupted(*args): raise RuntimeError('power loss')
    monkeypatch.setattr(controller.state, 'finish', interrupted)
    with pytest.raises(RuntimeError, match='power loss'):
        await controller.tick(epoch, filters)
    assert len(exchange.place_calls) == 1
    monkeypatch.setattr(controller.state, 'finish', original)
    restarted = TrailingController(engine)
    engine.trailing = restarted
    assert await restarted.tick(epoch, filters)
    assert len(exchange.place_calls) == 1
    assert restarted.state.window(epoch)['lower_index'] == 1
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_movement_limit_and_whole_grid_terminal_exit(moving):
    store, exchange, engine, controller, filters, epoch = moving
    for price in [150, 160, 170]:
        exchange.mark = D(price)
        assert await controller.tick(epoch, filters)
    assert controller.state.window(epoch)['upper_index'] == 7
    exchange.mark = D(180)
    assert await controller.tick(epoch, filters)
    assert controller.state.window(epoch)['upper_index'] == 7
    exchange.mark = D(190)
    assert not await controller.guard(epoch)
    assert store.active_epoch_id() is None and not await exchange.open_orders()
    assert store.stopped_epoch(epoch)
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_risk_cap_defers_move_and_never_cancels_reverse(moving):
    store, exchange, engine, controller, filters, epoch = moving
    store.conn.execute("UPDATE order_intent SET role='REVERSE' WHERE client_order_id='eg-old-0'")
    engine.limits = HardLimits(D(240), D(2400), 2, 5)
    assert await controller.tick(epoch, filters)
    assert not exchange.cancel_calls and not exchange.place_calls
    assert controller.state.window(epoch)['lower_index'] == 0
    assert store.engine_state()['state'] == 'ACTIVE'


def test_old_pair_prices_survive_signed_window_indices(moving):
    store, exchange, engine, controller, filters, epoch = moving
    state = controller.state
    store.record_order('eg-old-0', '1', 'FILLED', D('.1'))
    store.record_fill('eg-old-0', '1', '1', D('.1'), D(100))
    store.record_order('eg-old-1', '2', 'CANCELED', D(0))
    store.conn.execute('UPDATE grid_trailing SET lower_index=2,upper_index=6 WHERE epoch_id=?', (epoch,))
    prepared = FillCoordinator(store).prepare_reverse('eg-old-0', tuple(map(D, [100,110,120,130,140])), D('.001'), D('.001'), D(5))
    assert prepared.price == 110 and prepared.quantity == D('.1')
    store.record_order(prepared.client_order_id, '99', 'FILLED', D('.1'))
    store.record_fill(prepared.client_order_id, '99', '2', D('.1'), D(110))
    assert FillCoordinator(store).prepare_reverse(prepared.client_order_id, (), D('.001'), D('.001'), D(5)).status == 'RETIRED'
    assert state.level(epoch, -1) == 90
    assert state.level(epoch, 5) == 150


@pytest.mark.asyncio
async def test_same_state_hourly_signal_keeps_active_grid_without_replanning(moving, tmp_path):
    from strategies.grid.execution.main import ExecutorService
    from strategies.grid.execution.contracts import ExecutionSettings
    from strategies.grid.execution.risk import RiskAssessment, RiskStatus
    store, exchange, engine, controller, filters, epoch = moving
    exchange.mark = D(125)
    now = int(time.time() * 1000)
    original = json.loads(store.conn.execute('SELECT payload_json FROM decisions').fetchone()[0])
    original['calculated_at_ms'] = now + 1
    original['grid_params']['lower_boundary'] = '105'
    original['grid_params']['upper_boundary'] = '145'
    store.publish(original, 'new-hour')
    settings = ExecutionSettings(mode='live', max_margin_usdt=D(240), max_notional_usdt=D(2400),
                                 max_daily_loss_usdt=D(15), max_drawdown_usdt=D(30),
                                 max_open_orders=13, expected_leverage=5)
    service = ExecutorService(settings, store, exchange, {'GRID_TRAILING_ENABLED': 'true',
                             'GRID_EXECUTION_HEARTBEAT': str(tmp_path / 'heartbeat')})
    service.filters = filters; service.stream.ready = True
    service._risk = AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW, ()))
    service._plan = AsyncMock(side_effect=AssertionError('hourly refresh must not rebuild'))
    await asyncio.sleep(.005)
    await service.tick()
    service._plan.assert_not_awaited()
    assert store.next_decision() is None
    assert store.active_epoch_id() == epoch and store.engine_state()['state'] == 'ACTIVE'
    assert not exchange.cancel_calls and not exchange.place_calls
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_exit_signal_prevents_a_trailing_move(moving, tmp_path):
    from strategies.grid.execution.main import ExecutorService
    from strategies.grid.execution.contracts import ExecutionSettings
    from strategies.grid.execution.risk import RiskAssessment, RiskStatus
    store, exchange, engine, controller, filters, epoch = moving
    now = int(time.time() * 1000)
    original = json.loads(store.conn.execute('SELECT payload_json FROM decisions').fetchone()[0])
    original.update(calculated_at_ms=now + 1, state='NORMAL_STRONG_TREND', feasibility=None)
    store.publish(original, 'exit-signal')
    settings = ExecutionSettings(mode='live', max_margin_usdt=D(240), max_notional_usdt=D(2400),
                                 max_daily_loss_usdt=D(15), max_drawdown_usdt=D(30),
                                 max_open_orders=13, expected_leverage=5)
    service = ExecutorService(settings, store, exchange, {'GRID_TRAILING_ENABLED': 'true',
                             'GRID_EXECUTION_HEARTBEAT': str(tmp_path / 'heartbeat')})
    service.filters = filters; service.stream.ready = True
    service._risk = AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW, ()))
    await asyncio.sleep(.005)
    await service.tick()
    assert store.active_epoch_id() is None and store.engine_state()['state'] == 'FLAT'
    assert not exchange.place_calls
    assert store.next_decision() is None


@pytest.mark.asyncio
async def test_new_draft_and_move_stage_commit_together(moving, monkeypatch):
    store, exchange, engine, controller, filters, epoch = moving
    original = controller.state.stage
    def interrupt(move_id, stage, error=None):
        if stage == 'ADD_READY':
            raise RuntimeError('interruption before ready journal')
        return original(move_id, stage, error)
    monkeypatch.setattr(controller.state, 'stage', interrupt)
    with pytest.raises(RuntimeError, match='before ready'):
        await controller.tick(epoch, filters)
    assert not [o for o in store.active_orders() if o['client_order_id'].startswith('eg-m-')]
    monkeypatch.setattr(controller.state, 'stage', original)
    assert await engine.reconcile()
    assert await controller.tick(epoch, filters)
    assert len(exchange.place_calls) == 1
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_unsent_draft_and_lost_add_reply_without_double_post(moving, monkeypatch):
    store, exchange, engine, controller, filters, epoch = moving
    original = exchange.place_limit
    async def lost_reply(*args):
        await original(*args)
        raise TimeoutError('accepted order response lost')
    monkeypatch.setattr(exchange, 'place_limit', lost_reply)
    assert not await controller.tick(epoch, filters)
    assert controller.state.pending(epoch)['stage'] == 'SENDING'
    assert len(exchange.place_calls) == 1
    restarted = TrailingController(engine)
    engine.trailing = restarted
    assert await engine.reconcile()
    assert await restarted.tick(epoch, filters)
    assert len(exchange.place_calls) == 1
    assert restarted.state.window(epoch)['lower_index'] == 1
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_soft_terminal_cancels_unsent_move_draft_without_venue_delete(moving, monkeypatch):
    store, exchange, engine, controller, filters, epoch = moving
    original = controller.state.stage
    def interrupt(move_id, stage, error=None):
        result = original(move_id, stage, error)
        if stage == 'ADD_READY':
            raise RuntimeError('stopped after commit')
        return result
    monkeypatch.setattr(controller.state, 'stage', interrupt)
    with pytest.raises(RuntimeError):
        await controller.tick(epoch, filters)
    exchange.mark = D(190)
    assert not await controller.guard(epoch)
    assert store.engine_state()['state'] == 'FLAT'
    assert not store.active_orders()
    assert not exchange.place_calls
    assert exchange.cancel_calls == ['eg-old-0', 'eg-old-1']


@pytest.mark.asyncio
@pytest.mark.parametrize('price_after_cancel, restored', [(D(125), True), (D(90), False)])
async def test_reversal_aborts_shift_and_restores_only_passive_unfilled_edge(moving, monkeypatch, price_after_cancel, restored):
    store, exchange, engine, controller, filters, epoch = moving
    original = exchange.cancel_order
    async def retrace(client):
        result = await original(client)
        exchange.mark = price_after_cancel
        return result
    monkeypatch.setattr(exchange, 'cancel_order', retrace)
    assert await controller.tick(epoch, filters)
    assert controller.state.pending(epoch) is None
    window = controller.state.window(epoch)
    assert (window['lower_index'], window['upper_index']) == (0, 4)
    moves = [dict(r) for r in store.conn.execute('SELECT * FROM grid_move')]
    payload = json.loads(moves[-1]['payload_json'])
    assert payload['outcome'] == 'ABORTED'
    assert len(exchange.place_calls) == int(restored)
    if restored:
        order = store.get_order(exchange.place_calls[0])
        assert order['level_index'] == 0 and D(order['price']) == 100 and D(order['quantity']) == D('.1')
    else:
        assert payload['restore_reason'] == 'MARKETABLE'
        assert await controller.tick(epoch, filters)
        assert controller.state.window(epoch)['lower_index'] == -1
    assert store.engine_state()['state'] == 'ACTIVE'
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_retries_cancel_only_after_owned_live_query(moving, monkeypatch):
    store, exchange, engine, controller, filters, epoch = moving
    original = exchange.cancel_order
    called = [False]
    async def fail_before_http(client):
        if not called[0]:
            called[0] = True
            raise ConnectionError('request not sent')
        return await original(client)
    monkeypatch.setattr(exchange, 'cancel_order', fail_before_http)
    assert not await controller.tick(epoch, filters)
    restarted = TrailingController(engine); engine.trailing = restarted
    assert await restarted.tick(epoch, filters)
    assert exchange.cancel_calls == ['eg-old-0'] and len(exchange.place_calls) == 1
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_reverse_chain_retired_against_proposed_window_never_reopens(moving):
    store, exchange, engine, controller, filters, epoch = moving
    store.record_order('eg-old-0', '1', 'FILLED', D('.1'))
    store.record_fill('eg-old-0', '1', 'entry', D('.1'), D(100))
    exchange.orders['eg-old-0'].update(status='FILLED', executedQty='.1')
    store.record_order('eg-old-1', '2', 'CANCELED', D(0))
    exchange.orders['eg-old-1']['status'] = 'CANCELED'
    reverse = FillCoordinator(store).prepare_reverse('eg-old-0', tuple(map(D, [100,110,120,130,140])), D('.001'), D('.001'), D(5))
    store.record_order(reverse.client_order_id, '99', 'FILLED', D('.1'))
    store.record_fill(reverse.client_order_id, '99', 'exit', D('.1'), D(110))
    assert await controller.tick(epoch, filters)
    assert controller.state.window(epoch)['lower_index'] == 1
    assert all(o['level_index'] != 0 for o in store.active_orders())
    assert store.adjacent_level(store.get_order(reverse.client_order_id), ()) is None
    store.conn.execute('UPDATE grid_trailing SET lower_index=0,upper_index=4 WHERE epoch_id=?', (epoch,))
    assert store.adjacent_level(store.get_order(reverse.client_order_id), ()) is None
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_cancel_keeps_fill_quantity_and_original_pair_price(moving, monkeypatch):
    store, exchange, engine, controller, filters, epoch = moving
    original = exchange.cancel_order
    async def cancel_with_fill(client):
        exchange.orders[client]['executedQty'] = '.06'
        exchange.quantity = D('.06')
        exchange.trades.append({'symbol':'ETHUSDT', 'orderId':1, 'id':9, 'side':'BUY', 'qty':'.06', 'price':'100'})
        return await original(client)
    monkeypatch.setattr(exchange, 'cancel_order', cancel_with_fill)
    assert await controller.tick(epoch, filters)
    assert D(store.get_order('eg-old-0')['executed_qty']) == D('.06')
    assert sum((D(f['quantity']) for f in store.fills('eg-old-0')), D(0)) == D('.06')
    assert exchange.quantity == D('.06')
    assert len(await exchange.open_stops()) == 1
    assert store.adjacent_level(store.get_order('eg-old-0'), ()) == (1, D(110))
    assert 'eg-old-0' in engine.pending_obligations(epoch, (), filters)
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_reverse_is_adopted_without_cancel_or_duplicate_quantity(moving):
    store, exchange, engine, controller, filters, epoch = moving
    store.prepare_order(epoch, 5, 'ENTRY', 'SELL', D(150), D('.1'), 'eg-prior-short')
    store.record_order('eg-prior-short', '98', 'FILLED', D('.1'))
    store.record_fill('eg-prior-short', '98', '1', D('.1'), D(150))
    reverse = FillCoordinator(store).prepare_reverse('eg-prior-short', (), D('.001'), D('.001'), D(5))
    store.record_order(reverse.client_order_id, '99', 'NEW', D(0))
    exchange.orders[reverse.client_order_id] = {'symbol':'ETHUSDT', 'clientOrderId':reverse.client_order_id,
        'orderId':99, 'side':'BUY', 'price':'140', 'origQty':'.1', 'executedQty':'0', 'status':'NEW'}
    exchange.quantity = D('-.1')
    assert await controller.tick(epoch, filters)
    assert exchange.cancel_calls == ['eg-old-0']
    assert not exchange.place_calls
    assert store.get_order(reverse.client_order_id)['status'] == 'NEW'
    assert controller.state.window(epoch)['lower_index'] == 1
    assert len(await exchange.open_stops()) == 1
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_state_backlog_does_not_pause_and_rebuild(moving, tmp_path):
    from strategies.grid.execution.main import ExecutorService
    from strategies.grid.execution.contracts import ExecutionSettings
    from strategies.grid.execution.risk import RiskAssessment, RiskStatus
    store, exchange, engine, controller, filters, epoch = moving
    exchange.mark = D(125)
    original = json.loads(store.conn.execute('SELECT payload_json FROM decisions').fetchone()[0])
    now = int(time.time() * 1000)
    for i in [1, 2]:
        original['calculated_at_ms'] = now + i
        store.publish(original, 'queued-hour-' + str(i))
    settings = ExecutionSettings(mode='live', max_margin_usdt=D(240), max_notional_usdt=D(2400),
                                 max_daily_loss_usdt=D(15), max_drawdown_usdt=D(30), max_open_orders=13, expected_leverage=5)
    service = ExecutorService(settings, store, exchange, {'GRID_TRAILING_ENABLED':'true',
                              'GRID_EXECUTION_HEARTBEAT':str(tmp_path/'heartbeat')})
    service.filters = filters; service.stream.ready = True
    service._risk = AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW, ()))
    service._plan = AsyncMock(side_effect=AssertionError('backlog must not rebuild'))
    await asyncio.sleep(.005)
    await service.tick()
    service._plan.assert_not_awaited()
    assert store.next_decision() is None
    assert store.active_epoch_id() == epoch and store.engine_state()['state'] == 'ACTIVE'
    assert not exchange.cancel_calls and not exchange.place_calls
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_aborted_unsent_draft_never_reuses_original_client_after_budget_deferral(moving, monkeypatch):
    from strategies.grid.execution.planner import PlanLimitExceeded
    store, exchange, engine, controller, filters, epoch = moving
    mark_price = exchange.mark_price
    async def fail_before_send():
        pending = controller.state.pending(epoch)
        if pending and pending['stage'] == 'ADD_READY':
            raise TimeoutError('read failed after draft commit')
        return await mark_price()
    monkeypatch.setattr(exchange, 'mark_price', fail_before_send)
    assert not await controller.tick(epoch, filters)
    assert controller.state.pending(epoch)['stage'] == 'ADD_READY'
    original_client = controller.state.pending(epoch)['add_client_id']
    monkeypatch.setattr(exchange, 'mark_price', mark_price)
    budget = controller._budget
    async def insufficient_restore(shift, *args, **kwargs):
        if shift.direction == 'RESTORE':
            raise PlanLimitExceeded('temporarily reserved margin')
        return await budget(shift, *args, **kwargs)
    monkeypatch.setattr(controller, '_budget', insufficient_restore)
    exchange.mark = D(125)
    assert await controller.tick(epoch, filters)
    assert controller.state.pending(epoch)['stage'] == 'ABORTING'
    assert store.get_order(original_client)['status'] == 'CANCELED'
    exchange.mark = D(150)
    monkeypatch.setattr(controller, '_budget', budget)
    assert await controller.tick(epoch, filters)
    assert controller.state.pending(epoch) is None
    assert controller.state.window(epoch)['lower_index'] == 0
    assert len(exchange.place_calls) == 1 and exchange.place_calls[0].startswith('eg-mr-')
    assert await controller.tick(epoch, filters)
    assert controller.state.window(epoch)['lower_index'] == 1
    assert original_client not in exchange.place_calls
    assert store.engine_state()['state'] == 'ACTIVE'
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_replacement_protection_blocks_edge_post_before_reconcile_failure(moving, monkeypatch):
    from strategies.grid.execution.fills import ProtectionCoordinator
    store, exchange, engine, controller, filters, epoch = moving
    raw = json.loads(controller.state.window(epoch)['policy_json']); raw['move_down'] = None
    store.conn.execute('UPDATE grid_trailing SET policy_json=? WHERE epoch_id=?', (json.dumps(raw), epoch))
    store.record_order('eg-old-1', '2', 'FILLED', D('.1'))
    store.record_fill('eg-old-1', '2', 'owned', D('.1'), D(110))
    exchange.orders['eg-old-1'].update(status='FILLED', executedQty='.1')
    exchange.quantity = D('.1')
    assert await ProtectionCoordinator(store, exchange).ensure(epoch, exchange.quantity, D(50), D(190))
    original = exchange.place_stop
    async def malformed(side, trigger, client):
        row = await original(side, trigger, client)
        if trigger == 60:
            exchange.stops[client]['workingType'] = 'CONTRACT_PRICE'
            row['workingType'] = 'CONTRACT_PRICE'
        return row
    monkeypatch.setattr(exchange, 'place_stop', malformed)
    assert not await controller.tick(epoch, filters)
    assert store.engine_state()['state'] == 'LOCKED'
    assert not exchange.place_calls


@pytest.mark.asyncio
async def test_opposite_pair_at_add_level_does_not_strand_retraced_move(moving, monkeypatch):
    store, exchange, engine, controller, filters, epoch = moving
    store.prepare_order(epoch, 3, 'ENTRY', 'BUY', D(130), D('.1'), 'eg-old-fill')
    store.record_order('eg-old-fill', '99', 'FILLED', D('.1'))
    store.record_fill('eg-old-fill', '99', 'owned', D('.1'), D(130))
    exchange.quantity = D('.1')
    original = exchange.cancel_order
    async def retrace(client):
        row = await original(client); exchange.mark = D(125); return row
    monkeypatch.setattr(exchange, 'cancel_order', retrace)
    assert await controller.tick(epoch, filters)
    assert controller.state.pending(epoch) is None
    assert controller.state.window(epoch)['lower_index'] == 0
    added = [store.get_order(c) for c in exchange.place_calls]
    assert any(o['role'] == 'REVERSE' and o['side'] == 'SELL' and D(o['price']) == 140 for o in added)
    assert all(not (o['role'] == 'ENTRY' and o['side'] == 'BUY' and D(o['price']) == 140) for o in added)
    assert store.engine_state()['state'] == 'ACTIVE'
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_draft_cancellation_and_abort_intent_are_atomic_across_power_loss(moving, monkeypatch):
    store, exchange, engine, controller, filters, epoch = moving
    mark_price = exchange.mark_price
    async def stop_before_send():
        pending = controller.state.pending(epoch)
        if pending and pending['stage'] == 'ADD_READY':
            raise TimeoutError('before send')
        return await mark_price()
    monkeypatch.setattr(exchange, 'mark_price', stop_before_send)
    assert not await controller.tick(epoch, filters)
    monkeypatch.setattr(exchange, 'mark_price', mark_price)
    async def power_loss(*args):
        raise KeyboardInterrupt('simulated abrupt shutdown')
    monkeypatch.setattr(controller, '_abort_unsent', power_loss)
    exchange.mark = D(125)
    with pytest.raises(KeyboardInterrupt):
        await controller.tick(epoch, filters)
    pending = controller.state.pending(epoch)
    assert pending['stage'] == 'ABORTING'
    assert json.loads(pending['payload_json'])['outcome'] == 'ABORTED'
    exchange.mark = D(150)
    restarted = TrailingController(engine); engine.trailing = restarted
    assert await engine.reconcile()
    assert await restarted.tick(epoch, filters)
    assert restarted.state.pending(epoch) is None
    assert len(exchange.place_calls) == 1 and exchange.place_calls[0].startswith('eg-mr-')
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_settlement_during_replacement_never_reopens_grid(moving, monkeypatch):
    from strategies.grid.execution.fills import ProtectionCoordinator
    store, exchange, engine, controller, filters, epoch = moving
    raw = json.loads(controller.state.window(epoch)['policy_json']); raw['move_down'] = None
    store.conn.execute('UPDATE grid_trailing SET policy_json=? WHERE epoch_id=?', (json.dumps(raw), epoch))
    store.record_order('eg-old-1', '2', 'FILLED', D('.1'))
    store.record_fill('eg-old-1', '2', 'owned', D('.1'), D(110))
    exchange.orders['eg-old-1'].update(status='FILLED', executedQty='.1')
    exchange.quantity = D('.1')
    assert await ProtectionCoordinator(store, exchange).ensure(epoch, exchange.quantity, D(50), D(190))
    reconcile = engine.reconcile
    async def discover_stop(*args, **kwargs):
        result = await reconcile(*args, **kwargs)
        if D(store.epoch(epoch)['stop_loss_low']) == 60:
            store.settle_stop(epoch)
        return result
    monkeypatch.setattr(engine, 'reconcile', discover_stop)
    assert not await controller.tick(epoch, filters)
    assert store.stopped_epoch(epoch)
    assert store.engine_state()['state'] == 'EXITING'
    assert not exchange.place_calls
