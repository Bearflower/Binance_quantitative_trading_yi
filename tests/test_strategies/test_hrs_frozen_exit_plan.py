from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from strategies.hrs.position_manager import PositionManager


def plan():
    return {'version': 1, 'reference_price': 100.0, 'atr': 2.0, 'params': {
        'atr_multiplier': 3, 'emergency_percent': .1, 'min_absolute_percent': .01,
        'target1_atr_multiplier': 2, 'target1_close_percent': .3,
        'target2_atr_multiplier': 3, 'target2_close_percent': .4,
        'trailing_atr_multiplier': .5, 'max_holding_hours': 5}}


def test_frozen_plan_controls_trailing_deadline_and_survives_roundtrip():
    pm = PositionManager({}, AsyncMock())
    frozen = plan()
    opened = datetime.now(timezone.utc) - timedelta(hours=1)
    pm.add_position('TESTUSDT', 'long', 101, 10, 2, entry_mode='standard',
                    opened_at=opened, exit_plan=frozen)
    frozen['params']['trailing_atr_multiplier'] = 99
    pos = pm.get_position('TESTUSDT')
    pos.update(target2_reached=True, best_price=110)
    assert pm.check_trailing_stop('TESTUSDT', 108.9)
    assert pos['next_review_at'] == opened + timedelta(hours=5)
    pm.reset_time_stop('TESTUSDT')
    assert 4.99 < (pos['next_review_at']-datetime.now(timezone.utc)).total_seconds()/3600 <= 5
    restored = PositionManager({}, AsyncMock())
    restored.from_dict(deepcopy(pm.to_dict()))
    assert restored.get_position('TESTUSDT')['exit_plan'] == plan()


def test_invalid_frozen_plan_does_not_replace_existing_protection_with_defaults():
    pm = PositionManager({}, AsyncMock())
    with pytest.raises(ValueError, match='exit_plan'):
        pm.add_position('TESTUSDT', 'long', 100, 10, 2, exit_plan={'version': 1})


@pytest.mark.asyncio
async def test_frozen_plan_is_written_and_restored_in_position_row():
    from strategies.hrs.strategy import HRSStrategy
    strategy = object.__new__(HRSStrategy)
    strategy.db = AsyncMock()
    strategy.position_manager = PositionManager({}, AsyncMock())
    strategy.position_manager.add_position('TESTUSDT', 'long', 101, 10, 2,
                                          entry_mode='standard', exit_plan=plan())
    position = strategy.position_manager.get_position('TESTUSDT')
    await strategy._persist_position_row('TESTUSDT', position, 1794918147895)
    query, *values = strategy.db.execute.await_args.args
    assert 'exit_plan' in query
    import json
    assert json.loads(values[-1]) == plan()
    row = dict(symbol='TESTUSDT', direction='long', entry_price=101, quantity=10,
               opened_at=1794918147895, entry_mode='standard', exit_plan=json.dumps(plan()))
    restored = object.__new__(HRSStrategy)
    restored.position_manager = PositionManager({}, AsyncMock())
    restored._apply_position_row(row)
    assert restored.position_manager.get_position('TESTUSDT')['exit_plan'] == plan()


@pytest.mark.asyncio
async def test_replenishment_uses_frozen_reference_not_changed_config_or_fill_price():
    from strategies.hrs.executor import TradingExecutor
    executor = TradingExecutor({}, AsyncMock(), AsyncMock(), AsyncMock(), PositionManager({}, AsyncMock()))
    executor._get_symbol_precision = AsyncMock(return_value=(.01, .1))
    executor.binance_api.use_unified_account = True
    executor.binance_api.get_open_algo_orders = AsyncMock(return_value=[])
    executor.position_manager.add_position('TESTUSDT', 'long', 101, 10, 2)
    executor.binance_api.get_algo_order = AsyncMock(return_value=None)
    executor._replenish_all_roles = AsyncMock()
    await executor.replenish_position_orders('TESTUSDT', 'long', 101, 10, 99, exit_plan=plan())
    args = executor._replenish_all_roles.await_args.args
    assert args[2] == 100
    assert args[4] == 2
    assert args[7]['atr_multiplier'] == 3


def test_bad_persisted_plan_keeps_position_tracked_and_refuses_default_geometry():
    from strategies.hrs.strategy import HRSStrategy
    strategy = object.__new__(HRSStrategy)
    strategy.position_manager = PositionManager({}, AsyncMock())
    row = dict(symbol='TESTUSDT', direction='long', entry_price=101, quantity=10,
               opened_at=1794918147895, entry_mode='standard', exit_plan={'version': 1})
    strategy._apply_position_row(row)
    pos = strategy.position_manager.get_position('TESTUSDT')
    assert pos is not None
    assert pos['exit_plan_invalid'] is True
    assert pos['exit_plan'] == row['exit_plan']


def test_json_null_corruption_cannot_reenable_default_geometry():
    from strategies.hrs.strategy import HRSStrategy
    strategy = object.__new__(HRSStrategy)
    strategy.position_manager = PositionManager({}, AsyncMock())
    strategy._apply_position_row(dict(symbol='TESTUSDT', direction='long', entry_price=100,
        quantity=10, opened_at=1794918147895, entry_mode='standard', exit_plan='null'))
    pos = strategy.position_manager.get_position('TESTUSDT')
    assert pos['exit_plan_invalid'] is True
    with pytest.raises(ValueError, match='exit_plan'):
        strategy.position_manager._position_holding_hours(pos)


@pytest.mark.parametrize('field,value', [('reference_price','1e1000'), ('atr','1e1000'),
    ('max_holding_hours','1e300')])
def test_finite_decimal_cannot_become_infinite_float_or_overflow_deadline(field, value):
    from strategies.hrs.position_exit_plan import validate_exit_plan
    frozen = plan()
    if field in frozen:
        frozen[field] = value
    else:
        frozen['params'][field] = value
    with pytest.raises(ValueError, match='exit_plan'):
        validate_exit_plan(frozen)
