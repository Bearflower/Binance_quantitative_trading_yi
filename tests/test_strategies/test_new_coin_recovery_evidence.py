"""Restart evidence must never manufacture a fresh holding clock or submit exits."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from strategies.new_coin.strategy import NewCoinStrategy
from tests.test_strategies.test_take_profit_fill_detect import create_executor

SYMBOL = 'PFEUSDT'
OPENED = datetime(2026, 10, 30, 11, 1, 44, 496097)


def setup_recovery(rows=None, quantity=70):
    executor = create_executor()
    executor.db.fetch_all = AsyncMock(return_value=rows if rows is not None else [dict(
        symbol=SYMBOL, position_id='PFE-open-1', status='open', quantity='100',
        entry_price='25', opened_at=OPENED)])
    executor._get_symbol_precision = AsyncMock(return_value=(Decimal('.001'), Decimal('.01')))
    executor._precision_cache[SYMBOL] = (Decimal('.001'), Decimal('.01'))
    executor._step_size_for = lambda symbol: .001
    executor._close_position = AsyncMock()
    executor.binance_api.cancel_order = AsyncMock()
    strategy = NewCoinStrategy.__new__(NewCoinStrategy)
    rebuilt = {SYMBOL: dict(entry_price=25, exchange_entry_price=25, quantity=quantity,
                           entry_time=None)}
    return strategy, executor, rebuilt


@pytest.mark.asyncio
async def test_unique_db_open_restores_clock_anchor_and_tp_without_order_writes():
    strategy, executor, rebuilt = setup_recovery()
    await strategy._sync_baseline_to_tracking(executor, rebuilt, {SYMBOL: Decimal('2')})
    entry = executor.position_tracking[SYMBOL]
    assert entry['entry_time'] == OPENED.replace(tzinfo=timezone.utc)
    assert entry['tp_anchor_quantity'] == 100
    assert entry['target1_reached'] is False  # quantity decrease is not fill proof
    assert entry['recovery_target1_candidate'] is True
    assert entry['time_exit_status'] == 'review_required'
    assert rebuilt[SYMBOL]['entry_time'] == OPENED.replace(tzinfo=timezone.utc).isoformat()
    executor._close_position.assert_not_awaited()
    executor.binance_api.cancel_order.assert_not_awaited()
    # Repeating recovery cannot restart the clock or replace the original anchor.
    await strategy._sync_baseline_to_tracking(executor, rebuilt)
    assert entry['entry_time'] == OPENED.replace(tzinfo=timezone.utc)
    assert entry['tp_anchor_quantity'] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['missing', 'duplicate', 'future', 'nan', 'price', 'quantity', 'identity', 'db_error', 'local_price', 'overflow', 'wrong_symbol', 'missing_time', 'estimated_price'])
async def test_unproven_recovery_never_enters_time_exit(bad):
    strategy, executor, rebuilt = setup_recovery(quantity=100)
    rows = executor.db.fetch_all.return_value
    if bad == 'missing': rows.clear()
    elif bad == 'duplicate': rows.append(dict(rows[0]))
    elif bad == 'future': rows[0]['opened_at'] = datetime.now(timezone.utc) + timedelta(days=1)
    elif bad == 'nan': rows[0]['quantity'] = 'NaN'
    elif bad == 'price': rows[0]['entry_price'] = '26'
    elif bad == 'quantity': rows[0]['quantity'] = '10'
    elif bad == 'identity': rows[0]['position_id'] = None
    elif bad == 'db_error': executor.db.fetch_all.side_effect = RuntimeError('offline')
    elif bad == 'local_price': rebuilt[SYMBOL]['entry_price'] = 30
    elif bad == 'overflow': rows[0]['quantity'] = '1e400'
    elif bad == 'wrong_symbol': rows[0]['symbol'] = 'OTHERUSDT'
    elif bad == 'missing_time': rows[0]['opened_at'] = None
    elif bad == 'estimated_price': rebuilt[SYMBOL]['exchange_entry_price'] = None
    await strategy._sync_baseline_to_tracking(executor, rebuilt)
    entry = executor.position_tracking[SYMBOL]
    assert entry['time_exit_status'] == 'unknown'
    assert entry['tp_anchor_quantity'] is None
    executor._time_stop_review = AsyncMock(return_value=(True, 'would close'))
    await executor._check_time_stop(SYMBOL, OPENED.replace(tzinfo=timezone.utc))
    executor._time_stop_review.assert_not_awaited()
    executor._close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_precision_failure_keeps_time_exit_unknown():
    strategy, executor, rebuilt = setup_recovery()
    executor._get_symbol_precision.side_effect = RuntimeError('offline')
    await strategy._sync_baseline_to_tracking(executor, rebuilt)
    assert executor.position_tracking[SYMBOL]['time_exit_status'] == 'unknown'


def test_merge_does_not_manufacture_entry_time():
    from shared.position_baseline import build_short_positions
    strategy = NewCoinStrategy.__new__(NewCoinStrategy)
    strategy.positions = {}
    positions = build_short_positions([dict(symbol=SYMBOL, positionAmt='-70', entryPrice='25', markPrice='24')], 2)
    assert strategy._merge_rebuilt_positions(positions)[SYMBOL]['entry_time'] is None


@pytest.mark.asyncio
async def test_precision_fallback_is_not_recovery_evidence():
    strategy, executor, rebuilt = setup_recovery()
    executor._precision_cache.clear()  # real precision method returns defaults on errors
    await strategy._sync_baseline_to_tracking(executor, rebuilt)
    assert executor.position_tracking[SYMBOL]['time_exit_status'] == 'unknown'


@pytest.mark.asyncio
async def test_recovered_position_never_authorizes_time_exit_even_with_db_row():
    strategy, executor, rebuilt = setup_recovery(quantity=100)
    executor.time_stop_review_enabled = True
    executor._time_stop_review = AsyncMock(return_value=(True, 'would close'))
    await strategy._sync_baseline_to_tracking(executor, rebuilt)
    await executor._check_time_stop(SYMBOL, executor.position_tracking[SYMBOL]['entry_time'])
    executor._time_stop_review.assert_not_awaited()
    executor._close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_restored_tp1_skips_time_review_and_preserves_protection_state():
    strategy, executor, rebuilt = setup_recovery()
    executor.position_tracking[SYMBOL] = {'algo_ids': {'sl': 'protected'}, 'atr': 3}
    executor.time_stop_review_enabled = True
    executor._time_stop_review = AsyncMock(return_value=(True, 'close'))
    await strategy._sync_baseline_to_tracking(executor, rebuilt)
    entry = executor.position_tracking[SYMBOL]
    await executor._check_time_stop(SYMBOL, entry['entry_time'])
    assert entry['algo_ids'] == {'sl': 'protected'}
    assert entry['atr'] == 3
    executor._time_stop_review.assert_not_awaited()
    executor._close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_restart_chain_restores_db_time_with_no_exchange_writes():
    strategy, executor, _ = setup_recovery()
    strategy.positions = {}
    strategy.trading_executor = executor
    strategy.binance_client = executor.binance_api
    executor.binance_api.get_position = AsyncMock(return_value=[dict(
        symbol=SYMBOL, positionAmt='-70', entryPrice='25', markPrice='24')])
    executor.get_contract_size_map = AsyncMock(return_value={SYMBOL: 1})
    executor.get_open_short_symbols = AsyncMock(return_value={SYMBOL})
    executor._calculate_atr = AsyncMock(return_value=Decimal('2'))
    await strategy._rebuild_position_baseline()
    assert strategy.positions[SYMBOL]['entry_time'] == OPENED.replace(tzinfo=timezone.utc).isoformat()
    assert executor.position_tracking[SYMBOL]['target1_reached'] is False
    assert executor.position_tracking[SYMBOL]['recovery_target1_candidate'] is True
    assert executor.baseline_ready is True
    executor._close_position.assert_not_awaited()
    executor.binance_api.cancel_order.assert_not_awaited()
    executor.binance_api._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_entry_clock_still_honors_72_hour_boundary(monkeypatch):
    import strategies.new_coin.executor as module
    strategy, executor, rebuilt = setup_recovery(quantity=100)
    executor.time_stop_review_enabled = True
    executor._time_stop_review = AsyncMock(return_value=(False, 'hold'))
    opened = OPENED.replace(tzinfo=timezone.utc)
    executor.ensure_tracking_entry(SYMBOL, entry_price=25, entry_quantity=100, entry_time=opened)
    class Clock(datetime):
        current = opened + timedelta(hours=72, microseconds=-1)
        @classmethod
        def now(cls, tz=None): return cls.current
    monkeypatch.setattr(module, 'datetime', Clock)
    await executor._check_time_stop(SYMBOL, opened)
    executor._time_stop_review.assert_not_awaited()
    Clock.current = opened + timedelta(hours=72)
    await executor._check_time_stop(SYMBOL, opened)
    executor._time_stop_review.assert_awaited_once()
    executor._close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_time_does_not_disable_ordinary_protective_exit():
    _, executor, _ = setup_recovery()
    executor.position_tracking[SYMBOL] = dict(time_exit_status='unknown', atr=2,
                                            lowest_price=5, remaining_quantity=70)
    executor.binance_api._request.return_value = {'price': '10'}
    await executor._check_trailing_stop(SYMBOL)
    executor._close_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_observed_old_pfe_shape_never_automatically_runs_time_exit_review():
    strategy, executor, rebuilt = setup_recovery(quantity=3.28)
    row = executor.db.fetch_all.return_value[0]
    row.update(quantity='3.28', entry_price='30.41271340')
    rebuilt[SYMBOL].update(entry_price=30.41271340, exchange_entry_price=30.41271340)
    executor.time_stop_review_enabled = True
    executor._time_stop_review = AsyncMock(return_value=(True, 'would close'))
    await strategy._sync_baseline_to_tracking(executor, rebuilt, {SYMBOL: Decimal('1')})
    tracking = executor.position_tracking[SYMBOL]
    assert tracking['entry_time'] == OPENED.replace(tzinfo=timezone.utc)
    assert tracking['tp_anchor_quantity'] == 3.28
    assert tracking['remaining_quantity'] == 3.28
    assert tracking['target1_reached'] is False
    assert tracking['time_exit_status'] == 'review_required'
    executor._close_position.assert_not_awaited()
    executor.binance_api.cancel_order.assert_not_awaited()
    executor._time_stop_review.assert_not_awaited()
    await executor._check_time_stop(SYMBOL, tracking['entry_time'])
    executor._time_stop_review.assert_not_awaited()
    executor._close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_aware_db_timestamp_is_normalized_to_same_utc_instant():
    strategy, executor, rebuilt = setup_recovery()
    executor.db.fetch_all.return_value[0]['opened_at'] = OPENED.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    await strategy._sync_baseline_to_tracking(executor, rebuilt)
    assert executor.position_tracking[SYMBOL]['entry_time'] == OPENED.replace(tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_young_recovered_position_also_requires_fill_chain_review():
    strategy, executor, rebuilt = setup_recovery(quantity=100)
    executor.db.fetch_all.return_value[0]['opened_at'] = datetime.now(timezone.utc) - timedelta(hours=1)
    await strategy._sync_baseline_to_tracking(executor, rebuilt)
    assert executor.position_tracking[SYMBOL]['time_exit_status'] == 'review_required'
    executor._time_stop_review = AsyncMock(return_value=(True, 'would close'))
    await executor._check_time_stop(SYMBOL, OPENED.replace(tzinfo=timezone.utc))
    executor._time_stop_review.assert_not_awaited()
    executor._close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_clock_preserves_complete_position_management_price_paths():
    strategy, executor, rebuilt = setup_recovery(rows=[])
    await strategy._sync_baseline_to_tracking(executor, rebuilt, {SYMBOL: Decimal('2')})
    tracking = executor.position_tracking[SYMBOL]
    assert tracking['entry_time'] is None
    assert isinstance(tracking['emergency_entry_time'], datetime)
    executor._fetch_current_price_or_none = AsyncMock(return_value=Decimal('24'))
    executor._get_exchange_position_qty = AsyncMock(return_value=70)
    executor._check_emergency_stop = AsyncMock()
    executor._check_trailing_stop = AsyncMock()
    executor._check_dynamic_trailing = AsyncMock()
    tracking['target2_reached'] = True  # already-known price protection remains active
    executor._time_stop_review = AsyncMock(return_value=(True, 'would close'))
    executor.emergency_stop_enabled = True
    await executor.check_position_management(SYMBOL)
    executor._check_emergency_stop.assert_awaited_once_with(SYMBOL, tracking['emergency_entry_time'])
    executor._check_dynamic_trailing.assert_awaited_once_with(SYMBOL, Decimal('24'))
    executor._check_trailing_stop.assert_awaited_once_with(SYMBOL)
    executor._time_stop_review.assert_not_awaited()
    executor._close_position.assert_not_awaited()
    strategy.trading_executor = executor
    strategy._protection_gap_attempts = {}
    executor.find_missing_protection = AsyncMock(return_value=[])
    await strategy._guard_symbol(SYMBOL, {})
    executor.find_missing_protection.assert_awaited_once_with(
        SYMBOL, strict=True, target1_reached=False, target2_reached=True)


@pytest.mark.asyncio
async def test_unknown_time_still_allows_actual_emergency_price_rule():
    strategy, executor, rebuilt = setup_recovery(rows=[])
    await strategy._sync_baseline_to_tracking(executor, rebuilt)
    executor._fetch_current_price_or_none = AsyncMock(return_value=Decimal('30'))
    executor._get_exchange_position_qty = AsyncMock(return_value=70)
    executor.binance_api._request.return_value = {'price': '30'}
    executor.cancel_all_algo_orders = AsyncMock()
    executor.emergency_stop_enabled = True
    executor.emergency_stop_check_minutes = 15
    executor.emergency_stop_trigger_percent = Decimal('.10')
    executor._time_stop_review = AsyncMock(return_value=(True, 'would close'))
    await executor.check_position_management(SYMBOL)
    executor._close_position.assert_awaited_once_with(SYMBOL, executor.close_percent, '紧急止损')
    executor._time_stop_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_exchange_rebuild_still_blocks_restored_time_exit():
    strategy, executor, _ = setup_recovery()
    strategy.positions = {SYMBOL: {'entry_time': OPENED.isoformat(), 'quantity': 100}}
    strategy.trading_executor = executor
    strategy.binance_client = executor.binance_api
    executor.ensure_tracking_entry(SYMBOL, entry_price=25, entry_quantity=100,
                                   entry_time=OPENED.replace(tzinfo=timezone.utc))
    executor.get_contract_size_map = AsyncMock(return_value={})
    executor.binance_api.get_position = AsyncMock(side_effect=RuntimeError('offline'))
    await strategy._rebuild_position_baseline()
    assert executor.baseline_ready is False
    assert executor.position_tracking[SYMBOL]['time_exit_status'] == 'unknown'
    await executor._check_time_stop(SYMBOL, OPENED.replace(tzinfo=timezone.utc))
    executor._close_position.assert_not_awaited()
