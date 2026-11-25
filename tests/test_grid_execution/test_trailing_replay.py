"""Real-signal replay through the service, engine and durable trailing controller.

Only the account equity risk verdict is held at ALLOW to isolate order sequencing;
actual exposure/order limits, passivity, reconciliation and protection stay live.
The exchange is entirely in memory and this test never contacts Binance.
"""
import copy
import json
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.simulate_grid_snapshot import SimulatedExchange
from strategies.grid.execution.contracts import ExecutionSettings
from strategies.grid.execution.main import ExecutorService
from strategies.grid.execution.risk import RiskAssessment, RiskStatus
from strategies.grid.execution.store import DecisionStore, migrate
from strategies.grid.execution.trailing_plan import new_order_quantity, price_at


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def replay(tmp_path, monkeypatch):
    captured = json.loads((ROOT / 'tests/fixtures/grid_execution/rebuild_20261116.json').read_text())
    signal = json.loads((ROOT / 'tests/fixtures/grid_execution/ethusdt_20261116_1105.json').read_text())
    exchange = SimulatedExchange(captured)
    exchange.orders.clear()
    exchange.stops.clear()
    exchange.trades.clear()
    exchange.quantity = exchange.entry = exchange.realized = exchange.fees = D(0)
    exchange.mark = D(signal['payload']['market_analysis']['current_price'])
    exchange.clock = signal['payload']['calculated_at_ms'] + 3000
    monkeypatch.setattr('time.time', lambda: exchange.clock / 1000)
    path = tmp_path / 'journal.sqlite3'
    migrate(path)
    store = DecisionStore(path)
    settings = ExecutionSettings(
        mode='live', max_margin_usdt=D(240), max_notional_usdt=D(2400),
        max_open_orders=13, expected_leverage=5,
        max_daily_loss_usdt=D(15), max_drawdown_usdt=D(30))
    env = {'GRID_TRAILING_ENABLED': 'true',
           'GRID_EXECUTION_HEARTBEAT': str(tmp_path / 'heartbeat.json')}
    service = ExecutorService(settings, store, exchange, env)
    service.stream.ready = True
    service._risk = AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW, ()))
    exchange.close_position = AsyncMock(wraps=exchange.close_position)
    exchange.place_limit = AsyncMock(wraps=exchange.place_limit)
    store.publish(signal['payload'], signal['config_hash'])
    yield store, exchange, service, signal, settings, env
    store.close()


def assert_active(store, epoch):
    assert store.engine_state()['state'] == 'ACTIVE', store.engine_state()
    assert store.active_epoch_id() == epoch
    assert not [order for order in store.all_orders() if order['role'] == 'EXIT']


def assert_pairs_preserved(store, policy):
    pairs = store.conn.execute('SELECT * FROM pair').fetchall()
    for pair in pairs:
        source = store.get_order(pair['source_client_order_id'])
        reverse = store.get_order(pair['reverse_client_order_id'])
        expected = source['level_index'] + (1 if source['side'] == 'BUY' else -1)
        assert reverse['level_index'] == expected
        assert D(reverse['price']) == price_at(policy, expected)
        assert reverse['side'] != source['side']
        assert D(reverse['quantity']) == D(pair['quantity'])
        assert D(pair['quantity']) <= sum((D(f['quantity']) for f in store.fills(source['client_order_id'])), D(0))
    return len(pairs)


async def advance(service, exchange, store, epoch, target):
    """Fill crossed resting orders in price order, delivering their real events."""
    previous = exchange.mark
    rising = target > previous
    side = 'SELL' if rising else 'BUY'
    crossed = [order for order in await exchange.open_orders()
               if order['side'] == side and
               (previous < D(order['price']) <= target if rising
                else target <= D(order['price']) < previous)]
    crossed.sort(key=lambda order: D(order['price']), reverse=not rising)
    events = []
    for order in crossed:
        row = exchange.orders[order['clientOrderId']]
        assert row['status'] in ('NEW', 'PARTIALLY_FILLED')
        exchange.mark = D(row['price'])
        event = exchange.trade(row, D(row['origQty']) - D(row['executedQty']), exchange.mark, False)
        await service._on_event(event)
        assert_active(store, epoch)
        await service.engine.check_limits()
        events.append(event)
    exchange.mark = target
    await service.tick()
    assert_active(store, epoch)
    await service.engine.check_limits()
    return events


@pytest.mark.asyncio
async def test_real_signal_up_down_replay_keeps_epoch_protection_and_old_pairs(replay):
    store, exchange, service, signal, settings, env = replay
    assert service.trailing.state.window(1) is None
    await service.tick()
    epoch = store.active_epoch_id()
    assert epoch is not None
    assert_active(store, epoch)
    policy = service.trailing.state.policy(epoch)
    initial_window = service.trailing.state.window(epoch)
    assert (initial_window['lower_index'], initial_window['upper_index']) == (0, policy.grid_count)
    assert len(store.all_orders()) == policy.grid_count
    initial_orders = {o['client_order_id']: (o['price'], o['quantity']) for o in store.all_orders()}
    initial_levels = store.epoch(epoch)['levels_json']
    assert policy.raw_lower == D(signal['payload']['grid_params']['lower_boundary'])
    assert policy.raw_upper == D(signal['payload']['grid_params']['upper_boundary'])

    # A later valid same-state hourly recommendation carries different bounds.
    later = copy.deepcopy(signal['payload'])
    exchange.clock += 3_600_000
    later['calculated_at_ms'] = exchange.clock - 3000
    later['last_closed_kline_ms'] = {tf: value + 3_600_000
                                    for tf, value in later['last_closed_kline_ms'].items()}
    later['market_analysis']['last_closed_kline_ms'] = later['last_closed_kline_ms']
    for key in ('lower_boundary', 'upper_boundary'):
        later['grid_params'][key] = str(D(later['grid_params'][key]) + 10)
    store.publish(later, signal['config_hash'])
    await service.tick()
    assert_active(store, epoch)
    assert store.epoch(epoch)['levels_json'] == initial_levels
    assert service.trailing.state.policy(epoch) == policy
    assert {o['client_order_id']: (o['price'], o['quantity']) for o in store.all_orders()} == initial_orders

    upward = price_at(policy, policy.grid_count + 1)
    assert policy.raw_upper < upward <= policy.move_up < policy.stop_high
    up_events = await advance(service, exchange, store, epoch, upward)
    assert up_events
    assert exchange.quantity < 0
    stops = await exchange.open_stops()
    assert len(stops) == 1
    assert stops[0]['side'] == 'BUY'
    assert D(stops[0]['triggerPrice']) == policy.stop_high
    assert stops[0]['workingType'] == 'MARK_PRICE'
    assert stops[0]['closePosition'] is True
    window = service.trailing.state.window(epoch)
    assert (window['lower_index'], window['upper_index']) == (1, policy.grid_count + 1)
    assert service.trailing.state.pending(epoch) is None
    assert store.all_fills()
    assert assert_pairs_preserved(store, policy) > 0
    stops = await exchange.open_stops()
    assert any(stop['side'] == 'BUY' and D(stop['triggerPrice']) == policy.stop_high
               and stop['closePosition'] and stop['workingType'] == 'MARK_PRICE'
               for stop in stops)
    fill_count = len(store.all_fills())

    # Reconstruct the actual service against its durable journal. Duplicate WS
    # facts received after restart must not double fills, quantity, or submits.
    restarted = ExecutorService(settings, store, exchange, env)
    restarted.stream.ready = True
    restarted.filters = service.filters
    restarted._risk = AsyncMock(return_value=RiskAssessment(RiskStatus.ALLOW, ()))
    await restarted._on_reconnect()
    submitted = exchange.place_limit.call_count
    for event in reversed(up_events):
        await restarted._on_event(event)
    assert len(store.all_fills()) == fill_count
    assert exchange.place_limit.call_count == submitted
    assert_active(store, epoch)

    downward = price_at(policy, window['lower_index'] - 1)
    assert policy.stop_low < policy.move_down <= downward
    down_events = await advance(restarted, exchange, store, epoch, downward)
    assert down_events
    final = restarted.trailing.state.window(epoch)
    # This funded replay completes the reverse move without budget deferral.
    assert final['lower_index'] == 0
    assert final['upper_index'] - final['lower_index'] == policy.grid_count
    assert len(store.all_fills()) > fill_count
    assert_pairs_preserved(store, policy)
    assert store.epoch(epoch)['levels_json'] == initial_levels
    for client_id, original in initial_orders.items():
        order = store.get_order(client_id)
        assert (order['price'], order['quantity']) == original
    added_edges = [o for o in store.all_orders() if o['role'] == 'ENTRY'
                   and o['client_order_id'] not in initial_orders]
    assert added_edges
    for order in added_edges:
        assert D(order['quantity']) == new_order_quantity(policy, D(order['price']))
    if exchange.quantity:
        expected_side = 'SELL' if exchange.quantity > 0 else 'BUY'
        expected_trigger = policy.stop_low if exchange.quantity > 0 else policy.stop_high
        assert any(stop['side'] == expected_side and D(stop['triggerPrice']) == expected_trigger
                   for stop in await exchange.open_stops())
    assert len(await exchange.open_orders()) <= settings.max_open_orders
    assert len({o['orderId'] for o in exchange.orders.values()}) == len(exchange.orders)
    exchange.close_position.assert_not_called()
    await restarted.engine.check_limits()


@pytest.mark.asyncio
@pytest.mark.parametrize('next_state', ['OSCILLATION', 'EXTREME_STRONG_TREND'])
async def test_stream_pause_replays_original_orders_after_restart(replay, next_state):
    store, exchange, service, signal, settings, env = replay
    await service.tick()
    epoch = store.active_epoch_id()
    # Leave half a sell on the book. The paired buy and the residual sell
    # must both survive a disconnect, even across a process restart.
    order = min((o for o in exchange.orders.values() if o['side'] == 'SELL'),
                key=lambda o: D(o['price']))
    qty = D(order['origQty']) // D('0.002') * D('0.001')
    event = exchange.trade(order, qty, D(order['price']), False)
    order.update(executedQty=str(qty), status='PARTIALLY_FILLED')
    event['o']['X'] = 'PARTIALLY_FILLED'
    await service._on_event(event)
    def book(rows):
        return sorted((o['side'], D(o['price']), D(o['origQty']) - D(o['executedQty'])) for o in rows)
    original = book(await exchange.open_orders())
    assert exchange.quantity == -qty
    service.stream.ready = False
    await service._on_disconnect()
    assert not await exchange.open_orders()
    assert await exchange.open_stops()
    later = copy.deepcopy(signal['payload'])
    exchange.clock += 3_600_000
    later.update(state=next_state, calculated_at_ms=exchange.clock - 1000)
    later['last_closed_kline_ms'] = {tf: v + 3_600_000 for tf, v in later['last_closed_kline_ms'].items()}
    later['grid_params']['lower_boundary'] = str(D(later['grid_params']['lower_boundary']) + 1)
    store.publish(later, signal['config_hash'])
    restarted = ExecutorService(settings, store, exchange, env)
    restarted._risk = service._risk
    await restarted._on_reconnect()
    await restarted.tick()
    assert not await exchange.open_orders()
    restarted.stream.ready = True
    for _ in range(18):
        await restarted.tick()
    if next_state == 'OSCILLATION':
        assert_active(store, epoch)
        assert book(await exchange.open_orders()) == original
        assert await exchange.open_stops()
        assert exchange.quantity == -qty
        exchange.close_position.assert_not_called()
    else:
        assert store.active_epoch_id() is None
        assert exchange.quantity == 0
        exchange.close_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_recovery_requires_clean_reconciliation(replay):
    store, exchange, service, *_ = replay
    await service.tick()
    epoch = store.active_epoch_id()
    service.stream.ready = False
    await service._on_disconnect()
    await service._on_reconnect()
    exchange.place_limit.reset_mock()
    service.stream.ready = True
    service.engine.reconcile = AsyncMock(return_value=False)
    await service.tick()
    assert store.active_epoch_id() == epoch
    assert store.engine_state()['state'] == 'PAUSED'
    exchange.place_limit.assert_not_awaited()
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('index,side,price,reverse,initial_mark,next_mark', [
    (0, 'SELL', '1803.41', '1781.25', '1802.08', '1811.34'),
    (1, 'BUY', '1810.89', '1832.49', '1811.34', '1811.94'),
])
async def test_nov18_hourly_signal_after_disconnect(replay, index, side, price, reverse,
                                                     initial_mark, next_mark, caplog):
    caplog.set_level("INFO", logger="strategies.grid.execution")
    store, exchange, service, *_ = replay
    # Drop only the unused fixture signal before starting this simulated account.
    store.conn.execute('DELETE FROM decisions')
    signals = json.loads((ROOT / 'tests/fixtures/grid_execution/ethusdt_20261118_hourly.json').read_text())
    first, later = signals[index:index + 2]
    exchange.clock = first['payload']['calculated_at_ms'] + 5000
    exchange.mark = D(initial_mark)
    store.publish(first['payload'], first['config_hash'])
    await service.tick()
    epoch = store.active_epoch_id()
    target = next(o for o in exchange.orders.values() if D(o['price']) == D(price))
    assert target['side'] == side
    exchange.mark = D(price)
    await service._on_event(exchange.trade(target, D(target['origQty']), D(price), False))
    position = exchange.quantity
    exchange.mark = D(next_mark)
    exchange.clock = later['payload']['calculated_at_ms'] - 200_000
    service.stream.ready = False
    await service._on_disconnect()
    canceled_pair = next(o for o in store.all_orders() if o['role'] == 'REVERSE')
    assert any('grid order cancel resolved' in msg and 'trigger=user stream disconnected' in msg
               and 'role=REVERSE' in msg and f'price={reverse}' in msg
               and f'exchange_order_id={canceled_pair["exchange_order_id"]} status=CANCELED executed_qty=0' in msg
               for msg in caplog.messages)
    assert any('grid pause checked' in msg and f'position_qty={position}' in msg
               and 'reconciled=True' in msg for msg in caplog.messages)
    assert not await exchange.open_orders()
    exchange.clock = later['payload']['calculated_at_ms'] + 1000
    store.publish(later['payload'], later['config_hash'])
    await service._on_reconnect()
    service.stream.ready = True
    for _ in range(16):
        await service.tick()
    assert_active(store, epoch)
    assert exchange.quantity == position
    assert any(o['role'] == 'REVERSE' and D(o['price']) == D(reverse) for o in store.active_orders())
    assert any('grid resuming after stream reconciliation' in msg for msg in caplog.messages)
    assert sum('grid pair placed' in msg and f'price={reverse}' in msg for msg in caplog.messages) == 2
    exchange.close_position.assert_not_awaited()


def closed_mismatch(replay, reason='position does not match journal fills'):
    store, exchange, service, signal, *_ = replay
    # A settled short with both PM fills recorded in the durable journal.
    epoch = store.create_epoch('closed-signal')
    trades = []
    for i, (role, side, price) in enumerate([
        ('ENTRY', 'SELL', '1800.17'), ('EXIT', 'BUY', '1800.21'),
    ], start=1):
        cid = f'eg-closed-{i}'
        store.prepare_order(epoch, i, role, side, D(price), D('0.060'), cid)
        store.record_order(cid, str(i), 'FILLED', D('0.060'))
        store.record_fill(cid, str(i), str(i + 100), D('0.060'), D(price))
        trades.append({'symbol': 'ETHUSDT', 'orderId': i, 'id': i + 100, 'side': side,
                       'qty': '0.060', 'price': price, 'time': exchange.clock})
    store.set_engine_state('LOCKED', reason)
    store.close_epoch(epoch)
    exchange.trade_history = AsyncMock(return_value=trades)
    exchange.order_history = AsyncMock(return_value=[
        {'symbol': 'ETHUSDT', 'orderId': t['orderId'], 'clientOrderId': f'eg-closed-{t["orderId"]}',
         'side': t['side'], 'origQty': '0.060', 'executedQty': '0.060', 'status': 'FILLED',
         'time': exchange.clock, 'updateTime': exchange.clock}
        for t in trades
    ])
    return trades


@pytest.mark.asyncio
@pytest.mark.parametrize('blocker', [None, 'bad_event', 'missing_trade', 'open_stop'])
async def test_settled_late_exit_fill_lock_requires_exact_evidence(replay, blocker):
    store, exchange, service, *_ = replay
    trades = closed_mismatch(replay, 'fill handling uncertain')
    close = trades.pop()
    close['qty'] = '0.001'
    last = dict(close, id=103, qty='0.059')
    trades.extend([close, last])
    store.conn.execute("UPDATE fill SET quantity='0.001' WHERE trade_id='102'")
    store.record_fill('eg-closed-2', '2', '103', D('0.059'), D(close['price']))
    event = {'e': 'ORDER_TRADE_UPDATE', 'o': {
        's': 'ETHUSDT', 'c': 'eg-closed-2', 'i': 2, 'S': 'BUY',
        'x': 'TRADE', 'X': 'PARTIALLY_FILLED', 'l': '0.001', 'z': '0.001',
        'L': close['price'], 't': 102}}
    store.record_event_failure(event, ValueError('cumulative fill regressed' if blocker != 'bad_event'
                                                else 'conflicting duplicate trade ID'))
    if blocker == 'missing_trade':
        trades.remove(close)
    if blocker == 'open_stop':
        exchange.open_stops = AsyncMock(return_value=[{'clientAlgoId': 'pending'}])

    exchange.clock += 10_000
    await service.tick()
    assert store.engine_state()['state'] == 'LOCKED'
    exchange.clock += 5000
    await service.tick()
    assert store.engine_state()['state'] == ('LOCKED' if blocker else 'FLAT')
    if not blocker:
        assert exchange.trade_history.await_count == 2
        assert store.next_decision() is None
    exchange.place_limit.assert_not_awaited()
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_mismatch_auto_unlocks_and_waits_for_a_new_signal(replay):
    store, exchange, service, signal, *_ = replay
    closed_mismatch(replay)
    exchange.clock += 10_000
    await service.tick()
    assert store.engine_state()['state'] == 'LOCKED'
    exchange.clock += 5000
    await service.tick()
    assert store.engine_state()['state'] == 'FLAT'
    assert exchange.trade_history.await_count == 2
    assert store.next_decision() is None
    exchange.place_limit.assert_not_awaited()

    # An old calculation can arrive after the recovery is committed.
    delayed = copy.deepcopy(signal['payload'])
    delayed['calculated_at_ms'] = exchange.clock - 50
    store.publish(delayed, signal['config_hash'])
    assert store.next_decision() is None

    exchange.clock += 3_600_000
    fresh = copy.deepcopy(signal['payload'])
    fresh['calculated_at_ms'] = exchange.clock - 1000
    fresh['last_closed_kline_ms'] = {tf: v + 3_600_000 for tf, v in fresh['last_closed_kline_ms'].items()}
    store.publish(fresh, signal['config_hash'])
    await service.tick()
    assert store.engine_state()['state'] == 'ACTIVE'
    assert store.active_epoch_id() == 2
    assert exchange.place_limit.await_count
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('blocker', ['manual', 'risk', 'disconnected', 'foreign', 'foreign_order', 'missing', 'pending', 'unsafe_relock'])
async def test_closed_mismatch_cannot_unlock_without_all_recovery_evidence(replay, blocker):
    store, exchange, service, signal, *_ = replay
    trades = closed_mismatch(replay, 'risk: daily_usdt' if blocker == 'manual'
                             else 'position does not match journal fills')
    if blocker == 'risk':
        service._risk.return_value = RiskAssessment(RiskStatus.EXIT_LOCKED, ('daily_usdt',))
    elif blocker == 'disconnected':
        service.stream.ready = False
    elif blocker == 'foreign':
        # Two external fills can net to zero and still defeat a net-only check.
        trades.extend([dict(t, orderId=t['orderId'] + 10, id=t['id'] + 10) for t in trades.copy()])
    elif blocker == 'foreign_order':
        orders = exchange.order_history.return_value
        orders.append(dict(orders[0], orderId=90, clientOrderId='external', status='CANCELED', executedQty='0'))
    elif blocker == 'unsafe_relock':
        store.set_engine_state('LOCKED', 'stop cancellation uncertain')
        service.engine._lock('position does not match journal fills')
    elif blocker == 'missing':
        trades.pop()
    elif blocker == 'pending':
        store.prepare_order(1, 3, 'ENTRY', 'BUY', D('1700'), D('0.060'), 'eg-unknown')

    for _ in range(3):
        exchange.clock += 31_000
        await service.tick()
    assert store.engine_state()['state'] == 'LOCKED'
    exchange.place_limit.assert_not_awaited()
    exchange.close_position.assert_not_awaited()
    if blocker in ('manual', 'risk', 'disconnected', 'pending', 'unsafe_relock'):
        exchange.trade_history.assert_not_awaited()


def publish_refresh_signal(replay, state, *, hours=1, close_hours=None, shift=25):
    store, exchange, _, initial, *_ = replay
    exchange.clock += 3_600_000 * hours if hours else 3000
    signal = copy.deepcopy(initial['payload'])
    elapsed = (exchange.clock - signal['calculated_at_ms']) // 3_600_000 if close_hours is None else close_hours
    signal.update(state=state, calculated_at_ms=exchange.clock - 1000)
    signal['last_closed_kline_ms'] = {
        tf: close + elapsed * 3_600_000 for tf, close in signal['last_closed_kline_ms'].items()
    }
    for field in ('lower_boundary', 'upper_boundary', 'stop_loss_low', 'stop_loss_high',
                  'stop_move_up_price', 'stop_move_down_price'):
        value = signal['grid_params'].get(field)
        if value is not None:
            signal['grid_params'][field] = str(D(value) + shift)
    store.publish(signal, initial['config_hash'])


@pytest.mark.asyncio
async def test_grid_keeps_original_pair_and_bounds_during_tradable_state_churn(replay):
    store, exchange, service, initial, *_ = replay
    await service.tick()
    epoch = store.active_epoch_id()
    context = store.epoch(epoch)
    buy = max((o for o in exchange.orders.values() if o['side'] == 'BUY'), key=lambda o: D(o['price']))
    exchange.mark = D(buy['price'])
    await service._on_event(exchange.trade(buy, D(buy['origQty']), D(buy['price']), False))
    pair = next(o for o in store.active_orders() if o['role'] == 'REVERSE')
    other = 'WEAK_TREND' if initial['payload']['state'] == 'OSCILLATION' else 'OSCILLATION'
    for market_state in (other, initial['payload']['state'], other, other):
        publish_refresh_signal(replay, market_state)
        await service.tick()
        assert_active(store, epoch)
        assert store.epoch(epoch) == context
        assert store.get_order(pair['client_order_id'])['status'] == 'NEW'
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_flat_refresh_requires_distinct_hours_then_switches_without_market_close(replay):
    store, exchange, service, initial, *_ = replay
    await service.tick()
    epoch = store.active_epoch_id()
    other = 'WEAK_TREND' if initial['payload']['state'] == 'OSCILLATION' else 'OSCILLATION'
    publish_refresh_signal(replay, other)
    await service.tick()
    assert_active(store, epoch)
    publish_refresh_signal(replay, other, hours=0, close_hours=1)
    await service.tick()
    assert_active(store, epoch)
    publish_refresh_signal(replay, other)
    await service.tick()
    await service.tick()
    assert store.active_epoch_id() == epoch + 1
    assert store.epoch(epoch)['status'] == 'CLOSED'
    assert store.engine_state()['state'] == 'ACTIVE'
    assert not [o for o in store.active_orders() if o['epoch_id'] == epoch]
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_during_flat_refresh_restores_old_epoch_and_obligation(replay):
    store, exchange, service, initial, *_ = replay
    await service.tick()
    epoch = store.active_epoch_id()
    other = 'WEAK_TREND' if initial['payload']['state'] == 'OSCILLATION' else 'OSCILLATION'
    publish_refresh_signal(replay, other)
    await service.tick()
    assert_active(store, epoch)
    target = max((o for o in exchange.orders.values() if o['side'] == 'BUY'), key=lambda o: D(o['price']))
    real_cancel = exchange.cancel_order

    async def cancel_order(client_order_id):
        if client_order_id == target['clientOrderId']:
            exchange.mark = D(target['price'])
            exchange.trade(target, D(target['origQty']), D(target['price']), False)
            return dict(target)
        return await real_cancel(client_order_id)

    exchange.cancel_order = cancel_order
    publish_refresh_signal(replay, other)
    for _ in range(18):
        await service.tick()
    assert_active(store, epoch)
    assert exchange.quantity == D(target['origQty'])
    assert await exchange.open_stops()
    assert assert_pairs_preserved(store, service.trailing.state.policy(epoch)) >= 1
    assert any(o['role'] == 'REVERSE' for o in store.active_orders())
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('shifts', [(2, 2), (25, -25)])
async def test_refresh_ignores_small_or_unstable_geometry(replay, shifts):
    store, exchange, service, *_ = replay
    await service.tick()
    epoch = store.active_epoch_id()
    for shift in shifts:
        publish_refresh_signal(replay, 'WEAK_TREND', shift=shift)
        await service.tick()
        assert_active(store, epoch)
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_refresh_waits_until_old_pair_closes_position(replay):
    store, exchange, service, *_ = replay
    await service.tick()
    epoch = store.active_epoch_id()
    entry = max((o for o in exchange.orders.values() if o['side'] == 'BUY'), key=lambda o: D(o['price']))
    exchange.mark = D(entry['price'])
    await service._on_event(exchange.trade(entry, D(entry['origQty']), exchange.mark, False))
    for _ in range(2):
        publish_refresh_signal(replay, 'WEAK_TREND')
        await service.tick()
    assert_active(store, epoch)
    reverse = next(o for o in store.active_orders() if o['role'] == 'REVERSE')
    pair = exchange.orders[reverse['client_order_id']]
    exchange.mark = D(pair['price'])
    await service._on_event(exchange.trade(pair, D(pair['origQty']), exchange.mark, False))
    assert exchange.quantity == 0
    assert_active(store, epoch)
    # No third signal is needed after the old inventory settles.
    await service.tick()
    await service.tick()
    assert store.active_epoch_id() == epoch + 1
    assert store.epoch(epoch)['status'] == 'CLOSED'
    exchange.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_strong_trend_preempts_confirmed_candidate_waiting_for_flat(replay):
    store, exchange, service, *_ = replay
    await service.tick()
    epoch = store.active_epoch_id()
    entry = max((o for o in exchange.orders.values() if o['side'] == 'BUY'), key=lambda o: D(o['price']))
    exchange.mark = D(entry['price'])
    await service._on_event(exchange.trade(entry, D(entry['origQty']), exchange.mark, False))
    for _ in range(2):
        publish_refresh_signal(replay, 'WEAK_TREND')
        await service.tick()
    assert_active(store, epoch)
    publish_refresh_signal(replay, 'EXTREME_STRONG_TREND')
    await service.tick()
    assert store.epoch(epoch)['status'] == 'CLOSED'
    assert store.active_epoch_id() is None
    assert exchange.quantity == 0
    exchange.close_position.assert_awaited_once()
