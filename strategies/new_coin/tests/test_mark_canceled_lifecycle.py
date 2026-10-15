"""补测 executor.py AC-D2 mark_order_canceled / mark_open_orders_canceled 调用点。

缺口来源（覆盖率判定脚本识别）：
  L2680-2683  _cancel_trailing_stop_order 平仓触发 mark_order_canceled（try + except）
  L2736-2739  _sync_trailing_stop_order 撤旧 trailing 后 mark_order_canceled
  L2768-2771  _sync_trailing_stop_order 撤旧 SL 后 mark_order_canceled
  L3246-3250  cancel_all_algo_orders 成功撤单后 mark_open_orders_canceled

真实驱动：mock 实例（self） + patch 模块级 mark_* 函数，用 assert_awaited_once_with 做真断言，
并覆盖 side_effect=Exception → except 吞异常分支。
"""

import types
import pytest
from decimal import Decimal
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch


def _bind_method(ex, cls, method_name):
    return types.MethodType(getattr(cls, method_name), ex)


def _make_self():
    """self mock（仅含 mark_* 测试所需最小属性）。"""
    from strategies.new_coin import executor as executor_module

    class _Self:
        position_tracking: Dict[str, Dict[str, Any]] = {}
        db = MagicMock()
        db.fetch_all = AsyncMock()
        db.execute = AsyncMock()
        binance_api = MagicMock()
        binance_api.cancel_algo_order = AsyncMock(return_value={})
        config = {'trading': {'dynamic_trailing': {'cleanup_silent_error_codes': []}}}

    ex = _Self()
    ex._cancel_trailing_stop_order = _bind_method(
        ex, executor_module.TradingExecutor, '_cancel_trailing_stop_order'
    )
    ex._sync_trailing_stop_order = _bind_method(
        ex, executor_module.TradingExecutor, '_sync_trailing_stop_order'
    )
    ex.cancel_all_algo_orders = _bind_method(
        ex, executor_module.TradingExecutor, 'cancel_all_algo_orders'
    )
    return ex


# =============================================================================
# AC-D2：_cancel_trailing_stop_order（已通过，保留）
# =============================================================================


@pytest.mark.asyncio
async def test_cancel_trailing_stop_marks_canceled_success():
    """平仓触发撤 trailing → mark_order_canceled(db, algo_id=old_id) 被调一次。"""
    ex = _make_self()
    ex.position_tracking['TESTUSDT'] = {
        'algo_ids': {'trailing_stop': 'TRAIL_001'},
    }

    with patch(
        'strategies.new_coin.executor.mark_order_canceled',
        new=AsyncMock(),
    ) as mock_mark:
        await ex._cancel_trailing_stop_order('TESTUSDT')

    mock_mark.assert_awaited_once_with(ex.db, algo_id='TRAIL_001')
    ex.binance_api.cancel_algo_order.assert_awaited_once_with('TESTUSDT', 'TRAIL_001')


@pytest.mark.asyncio
async def test_cancel_trailing_stop_mark_exception_swallowed():
    """mark_order_canceled 抛异常 → except 吞掉，algo_ids.trailing_stop 仍被清 None。"""
    ex = _make_self()
    ex.position_tracking['TESTUSDT'] = {
        'algo_ids': {'trailing_stop': 'TRAIL_002'},
    }

    with patch(
        'strategies.new_coin.executor.mark_order_canceled',
        new=AsyncMock(side_effect=Exception("DB write failed")),
    ):
        await ex._cancel_trailing_stop_order('TESTUSDT')

    assert ex.position_tracking['TESTUSDT']['algo_ids']['trailing_stop'] is None


# =============================================================================
# AC-D2：_sync_trailing_stop_order 撤旧 trailing + 撤旧 SL
# mark_order_canceled 调用点在 L2736-2739（trailing）和 L2768-2771（SL）
# 后续还有 _format_price (L2783)，我们 mock 掉 self._format_price 避免 AttributeError
# =============================================================================


def _setup_sync_trailing_ex(mode: str):
    """构造 _sync_trailing_stop_order 所需的完整 mock self。"""
    from strategies.new_coin import executor as executor_module

    class _ST:
        position_tracking: Dict[str, Dict[str, Any]] = {}
        db = MagicMock()
        db.execute = AsyncMock()
        binance_api = MagicMock()
        binance_api.cancel_algo_order = AsyncMock(return_value={})
        binance_api.place_new_algo_order = AsyncMock(return_value={'algoId': 'NEW_001'})
        config = {'trading': {'dynamic_trailing': {'cleanup_silent_error_codes': []}}}
        stop_offset_pct = Decimal('0.005')

    ex = _ST()
    ex._sync_trailing_stop_order = _bind_method(
        ex, executor_module.TradingExecutor, '_sync_trailing_stop_order'
    )
    # 给 _sync_trailing_stop_order 后续步骤所需的 mock 属性
    ex._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))
    ex._format_price = lambda p, tick: p
    ex._format_quantity = lambda q, step: q
    ex.ensure_tracking_entry = lambda *a, **kw: {}
    ex._update_tracking_algo_ids = MagicMock()

    if mode == 'old_trailing_only':
        ex.position_tracking['TESTUSDT'] = {
            'algo_ids': {'trailing_stop': 'OLD_TRAIL_999', 'sl': None},
        }
    elif mode == 'old_sl_only':
        ex.position_tracking['TESTUSDT'] = {
            'algo_ids': {'trailing_stop': None, 'sl': 'OLD_SL_888'},
        }
    elif mode == 'old_sl_exception':
        ex.position_tracking['TESTUSDT'] = {
            'algo_ids': {'trailing_stop': None, 'sl': 'OLD_SL_777'},
        }
    return ex


@pytest.mark.asyncio
async def test_sync_trailing_cancels_old_trailing_and_marks():
    """旧 trailing 存在 → mark_order_canceled(trailing_id) 被调。"""
    ex = _setup_sync_trailing_ex('old_trailing_only')

    with patch(
        'strategies.new_coin.executor.mark_order_canceled',
        new=AsyncMock(),
    ) as mock_mark:
        await ex._sync_trailing_stop_order('TESTUSDT', Decimal('105'))

    calls = mock_mark.await_args_list
    assert any(c.kwargs.get('algo_id') == 'OLD_TRAIL_999' for c in calls), \
        f"期望 mark_order_canceled(algo_id='OLD_TRAIL_999')；实际 calls={calls}"


@pytest.mark.asyncio
async def test_sync_trailing_cancels_old_sl_and_marks():
    """trailing_stop=None 旧 SL 存在 → mark_order_canceled(sl_id) 被调。"""
    ex = _setup_sync_trailing_ex('old_sl_only')

    with patch(
        'strategies.new_coin.executor.mark_order_canceled',
        new=AsyncMock(),
    ) as mock_mark:
        await ex._sync_trailing_stop_order('TESTUSDT', Decimal('105'))

    calls = mock_mark.await_args_list
    assert any(c.kwargs.get('algo_id') == 'OLD_SL_888' for c in calls), \
        f"期望 mark_order_canceled(algo_id='OLD_SL_888')；实际 calls={calls}"


@pytest.mark.asyncio
async def test_sync_trailing_mark_exception_swallowed():
    """mark_order_canceled（撤旧 SL 路径）抛异常 → except 吞掉，algo_ids['sl'] 仍置 None。"""
    ex = _setup_sync_trailing_ex('old_sl_exception')

    with patch(
        'strategies.new_coin.executor.mark_order_canceled',
        new=AsyncMock(side_effect=Exception("DB down")),
    ):
        await ex._sync_trailing_stop_order('TESTUSDT', Decimal('105'))

    assert ex.position_tracking['TESTUSDT']['algo_ids']['sl'] is None


# =============================================================================
# AC-D2：cancel_all_algo_orders（已通过，保留）
# =============================================================================


@pytest.mark.asyncio
async def test_cancel_all_marks_open_orders_canceled_success():
    """algo_ids 内有 tp1/tp2 → 都成功撤单 → mark_open_orders_canceled 被调。"""
    ex = _make_self()
    ex.position_tracking['TESTUSDT'] = {
        'algo_ids': {'tp1': 'A', 'tp2': 'B'},
    }
    ex.binance_api.cancel_algo_order.reset_mock()

    with patch(
        'strategies.new_coin.executor.mark_open_orders_canceled',
        new=AsyncMock(),
    ) as mock_mark_all:
        result = await ex.cancel_all_algo_orders('TESTUSDT')

    assert result['cancelled'] == 2, f"期望 cancelled=2，实际 {result}"
    mock_mark_all.assert_awaited_once_with(ex.db, 'new_coin', 'TESTUSDT')


@pytest.mark.asyncio
async def test_cancel_all_mark_exception_swallowed():
    """mark_open_orders_canceled 抛异常 → except 吞掉，cancelled 仍被正确统计。"""
    ex = _make_self()
    ex.position_tracking['TESTUSDT'] = {
        'algo_ids': {'tp1': 'A'},
    }

    with patch(
        'strategies.new_coin.executor.mark_open_orders_canceled',
        new=AsyncMock(side_effect=Exception("DB write failed")),
    ):
        result = await ex.cancel_all_algo_orders('TESTUSDT')

    assert result['cancelled'] == 1

@pytest.mark.asyncio
async def test_sync_trailing_mark_exception_on_trailing_swallowed():
    """撤旧 trailing 时 mark_order_canceled 抛异常 → except 吞掉（L2738-2739）。"""
    ex = _setup_sync_trailing_ex('old_trailing_only')

    with patch(
        'strategies.new_coin.executor.mark_order_canceled',
        new=AsyncMock(side_effect=Exception("DB down")),
    ):
        await ex._sync_trailing_stop_order('TESTUSDT', Decimal('105'))

    # 主流程仍把 algo_ids['trailing_stop'] 置 None
    assert ex.position_tracking['TESTUSDT']['algo_ids']['trailing_stop'] is None
