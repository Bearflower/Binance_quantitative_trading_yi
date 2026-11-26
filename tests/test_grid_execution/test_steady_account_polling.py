"""Bound REST weight without relaxing per-tick boundaries or pre-submit checks."""
import json
import time
from collections import Counter
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from strategies.grid.execution import main
from strategies.grid.execution.contracts import ExecutionSettings
from strategies.grid.execution.main import ExecutorService
from strategies.grid.execution.planner import HardLimits
from tests.test_grid_execution.test_trailing_execution import moving


@pytest.fixture
def steady(moving, monkeypatch, tmp_path):
    store, exchange, _, _, filters, epoch = moving
    exchange.mark = D(125)
    exchange.pm = False
    async def account():
        return {'totalAvailableBalance': '300', 'accountEquity': '300',
                'positionInitialMargin': '0', 'openOrderInitialMargin': '4.2'}
    async def income(start, end):
        return []
    exchange.account_info, exchange.income = account, income
    counts = Counter()
    for name in ('position', 'mark_price', 'account_info', 'open_orders', 'open_stops',
                 'income', 'verify_account', 'place_limit', 'cancel_order'):
        original = getattr(exchange, name)
        async def counted(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return await _original(*args, **kwargs)
        setattr(exchange, name, counted)
    settings = ExecutionSettings(mode='live', max_margin_usdt=D(240),
        max_notional_usdt=D(2400), max_daily_loss_usdt=D(15), max_drawdown_usdt=D(30),
        max_open_orders=13, expected_leverage=5)
    service = ExecutorService(settings, store, exchange, {
        'GRID_TRAILING_ENABLED': 'true', 'GRID_EXECUTION_HEARTBEAT': str(tmp_path/'heartbeat')})
    service.filters = filters
    service.stream.ready = True
    service.signal_receipts = None
    clock = SimpleNamespace(now=0.0)
    wall = time.time()
    monkeypatch.setattr(main, 'time', SimpleNamespace(
        monotonic=lambda: clock.now, time=lambda: wall + clock.now))
    return service, exchange, store, epoch, clock, counts


async def warm(steady):
    service, _, store, _, _, counts = steady
    await service.tick()
    assert store.engine_state()['state'] == 'ACTIVE'
    assert counts['open_orders'] and counts['open_stops']  # first tick must prove ownership
    counts.clear()


def receipt(status='NEW', kind='NEW'):
    return {'e': 'ORDER_TRADE_UPDATE', 'o': {
        's': 'ETHUSDT', 'c': 'eg-old-0', 'i': 1, 'S': 'BUY', 'x': kind, 'X': status,
        'l': '0', 'z': '0', 'L': '0', 't': 0}}


@pytest.mark.asyncio
async def test_quiet_ready_grid_caps_full_account_polls_at_thirty_seconds(steady):
    service, _, store, _, clock, counts = steady
    await warm(steady)
    for _ in range(12):
        clock.now += 5
        await service.tick()
    assert store.engine_state()['state'] == 'ACTIVE'
    assert counts['open_orders'] <= 4  # full reconcile + exposure at 30s and 60s
    assert counts['open_stops'] == 2
    assert counts['position'] <= 4
    assert counts['mark_price'] >= 12
    assert counts['place_limit'] == 0


@pytest.mark.asyncio
async def test_new_zero_receipt_does_not_reopen_full_account_poll_window(steady):
    service, _, _, _, clock, counts = steady
    await warm(steady)
    await service._on_event(receipt())
    clock.now = 5
    await service.tick()
    assert counts['open_orders'] == counts['open_stops'] == 0
    assert counts['mark_price'] > 0


@pytest.mark.asyncio
async def test_terminal_event_reconciles_immediately_and_invalidates_quiet_window(steady):
    service, exchange, store, _, clock, counts = steady
    await warm(steady)
    exchange.orders['eg-old-0']['status'] = 'CANCELED'
    await service._on_event(receipt('CANCELED', 'CANCELED'))
    assert counts['open_orders'] and counts['open_stops']
    assert store.get_order('eg-old-0')['status'] == 'CANCELED'
    counts.clear()
    clock.now = 5
    await service.tick()
    assert counts['open_orders'] and counts['open_stops']


@pytest.mark.asyncio
async def test_boundary_stop_is_checked_on_five_second_light_tick(steady):
    service, exchange, store, epoch, clock, _ = steady
    await warm(steady)
    exchange.mark = D(191)
    clock.now = 5
    await service.tick()
    assert store.stopped_epoch(epoch)
    assert not store.active_orders()
    assert not exchange.place_calls


@pytest.mark.asyncio
async def test_mark_risk_growth_pauses_on_light_tick_before_any_order(steady):
    service, exchange, store, _, clock, _ = steady
    await warm(steady)
    service.engine.limits = HardLimits(D(6), D(30), 13, 5)
    exchange.mark = D(180)
    clock.now = 5
    await service.tick()
    assert store.engine_state()['state'] == 'PAUSED'
    assert not store.active_orders()
    assert not exchange.place_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('trigger', ['decision', 'not_ready', 'account_event', 'clock_regressed'])
async def test_invalidation_requires_full_account_evidence(steady, trigger):
    service, _, store, _, clock, counts = steady
    await warm(steady)
    clock.now = 5
    if trigger == 'decision':
        payload = json.loads(store.conn.execute('SELECT payload_json FROM decisions').fetchone()[0])
        payload['calculated_at_ms'] += 1000
        store.publish(payload, 'new-decision')
    elif trigger == 'not_ready':
        service.stream.ready = False
    elif trigger == 'account_event':
        await service._on_event({'e': 'ACCOUNT_UPDATE', 'a': {'m': 'ORDER'}})
    else:
        clock.now = -1
    await service.tick()
    assert counts['open_orders'] > 0


@pytest.mark.asyncio
async def test_movement_rechecks_all_orders_before_submitting(steady):
    service, exchange, _, _, clock, counts = steady
    await warm(steady)
    original = exchange.place_limit
    async def submit(*args):
        assert counts['open_orders'] > 0 and counts['open_stops'] > 0
        return await original(*args)
    exchange.place_limit = submit
    exchange.mark = D(150)
    clock.now = 5
    await service.tick()
    assert exchange.place_calls


@pytest.mark.asyncio
async def test_reconnect_forces_complete_account_check(steady):
    service, _, _, _, clock, counts = steady
    await warm(steady)
    await service._on_reconnect()
    assert counts['open_orders'] and counts['open_stops']
    counts.clear()
    clock.now = 5
    await service.tick()
    assert counts['open_orders'] and counts['open_stops']


@pytest.mark.asyncio
async def test_wrong_side_new_receipt_cannot_reuse_verified_quiet_window(steady):
    service, _, store, _, clock, _ = steady
    await warm(steady)
    event = receipt()
    event['o']['S'] = 'SELL'  # same owned client/order identity, conflicting side
    await service._on_event(event)
    assert store.engine_state()['state'] == 'LOCKED'
    clock.now = 5
    await service.tick()
    assert store.engine_state()['state'] == 'LOCKED'
