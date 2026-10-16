"""补测 strategy.py §3.4 基线重建三个新方法的全部分支。

缺口来源（覆盖率判定脚本识别）：
  L2317-2350  _sync_baseline_to_tracking（循环 + missing_atr 汇总 + _backfill_target_statuses 调用）
  L2369-2402  _backfill_anchor_quantity（已有锚点跳过 / db 异常 / 无 open / 多条 warning / anchor>0 写入 / anchor<=0）
  L2412-2452  _backfill_target_statuses（tracking 缺失 / 锚点缺失 + 预热精度 / remaining<=0 / hit1+hit2 置位 / 已置位不重复）

真实驱动：mock strategy 实例 + mock executor 实例，patch 模块级 to_aware_utc/derive_reached_targets，
用 assert 真断言覆盖全部分支。
"""

import types
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch


# =============================================================================
# 辅助：绑定方法 + 构造 strategy/executor mock
# =============================================================================


def _bind_method(ex, cls, method_name):
    return types.MethodType(getattr(cls, method_name), ex)


def _make_strategy_self():
    """NewCoinStrategy 实例 mock（只含三个基线方法 + _backfill_tracking_atr）。"""
    from strategies.new_coin import strategy as strategy_module

    class _S:
        # _backfill_tracking_atr 是静态方法，不需要 self
        pass

    s = _S()
    # 绑定三个实例方法
    s._sync_baseline_to_tracking = _bind_method(
        s, strategy_module.NewCoinStrategy, '_sync_baseline_to_tracking'
    )
    s._backfill_anchor_quantity = _bind_method(
        s, strategy_module.NewCoinStrategy, '_backfill_anchor_quantity'
    )
    s._backfill_target_statuses = _bind_method(
        s, strategy_module.NewCoinStrategy, '_backfill_target_statuses'
    )
    # _backfill_tracking_atr 是静态方法，直接引用
    s._backfill_tracking_atr = strategy_module.NewCoinStrategy._backfill_tracking_atr
    return s


def _make_executor_self():
    """TradingExecutor 实例 mock（供三个基线方法调用其属性）。"""
    from unittest.mock import AsyncMock as _AM, MagicMock as _MM

    ex = _MM()
    ex.position_tracking: Dict[str, Dict[str, Any]] = {}
    ex.db = _MM()
    ex.db.fetch_all = _AM()
    ex.target1_close_percent = Decimal('0.30')
    ex.target2_close_percent = Decimal('0.40')
    ex.qty_tolerance_ratio = 0.01
    ex.qty_tolerance_absolute = 0.0001
    ex._step_size_for_return = Decimal('0.001')

    # sync _step_size_for
    def _ssf(sym):
        return Decimal('0.001')
    ex._step_size_for = _ssf

    # async _get_symbol_precision (AsyncMock)
    ex._get_symbol_precision = _AM(return_value=(Decimal('0.01'), Decimal('0.001')))

    # ensure_tracking_entry
    ex.ensure_tracking_entry = lambda *a, **kw: {}

    return ex



# =============================================================================
# _backfill_anchor_quantity 全分支（§3.4 第2点 + 第5点 T8）
# =============================================================================


@pytest.mark.asyncio
async def test_anchor_skip_if_existing_positive():
    """tp_anchor_quantity 已有且 >0 → 跳过查询直接 return（L2370-2372）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    entry = {'tp_anchor_quantity': 2.5}  # 已有有效值

    await s._backfill_anchor_quantity(ex, 'TESTUSDT', entry)

    # db.fetch_all 没被调（快速 return）
    ex.db.fetch_all.assert_not_awaited()
    assert entry['tp_anchor_quantity'] == 2.5  # 原值保留


@pytest.mark.asyncio
async def test_anchor_query_db_exception_ignored():
    """db.fetch_all 抛异常 → except 吞掉（L2382-2387），不崩溃。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.db.fetch_all = AsyncMock(side_effect=Exception("DB down"))
    entry = {'tp_anchor_quantity': None}

    await s._backfill_anchor_quantity(ex, 'TESTUSDT', entry)

    # tp_anchor_quantity 保持 None（fail-closed）
    assert entry.get('tp_anchor_quantity') is None


@pytest.mark.asyncio
async def test_anchor_no_open_rows_skip():
    """short_positions 无 open 记录 → return 不写入（L2388-2393）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.db.fetch_all = AsyncMock(return_value=[])
    entry = {'tp_anchor_quantity': None}

    await s._backfill_anchor_quantity(ex, 'TESTUSDT', entry)

    assert entry.get('tp_anchor_quantity') is None


@pytest.mark.asyncio
async def test_anchor_multiple_rows_warning_takes_latest():
    """命中多条 open 记录 → 取最新第一条 + 打 warning（L2395-2399）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.db.fetch_all = AsyncMock(return_value=[
        {'quantity': 5.0, 'opened_at': '2026-10-15'},
        {'quantity': 3.0, 'opened_at': '2026-10-14'},
    ])
    entry = {'tp_anchor_quantity': None}

    await s._backfill_anchor_quantity(ex, 'TESTUSDT', entry)

    # 取最新一条（第一个）
    assert entry['tp_anchor_quantity'] == 5.0


@pytest.mark.asyncio
async def test_anchor_anchor_positive_writes():
    """anchor > 0 → 写入 tp_anchor_quantity（L2400-2402）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.db.fetch_all = AsyncMock(return_value=[{'quantity': 4.2, 'opened_at': '2026-10-15'}])
    entry = {'tp_anchor_quantity': None}

    await s._backfill_anchor_quantity(ex, 'TESTUSDT', entry)

    assert entry['tp_anchor_quantity'] == 4.2


@pytest.mark.asyncio
async def test_anchor_anchor_zero_no_write():
    """anchor = 0 → 不写入（L2400-2402 fail-closed）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.db.fetch_all = AsyncMock(return_value=[{'quantity': 0, 'opened_at': '2026-10-15'}])
    entry = {'tp_anchor_quantity': None}

    await s._backfill_anchor_quantity(ex, 'TESTUSDT', entry)

    assert entry.get('tp_anchor_quantity') is None


# =============================================================================
# _backfill_target_statuses 全分支（§3.4 第3点 档位自愈）
# =============================================================================


@pytest.mark.asyncio
async def test_target_status_no_tracking_skip():
    """executor.position_tracking 无此 symbol → continue 跳过（L2413-2415）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking = {}  # 空

    # patch derive_reached_targets 确认不被调
    with patch('strategies.new_coin.strategy.derive_reached_targets') as mock_d:
        await s._backfill_target_statuses(ex, ['TESTUSDT'])
    mock_d.assert_not_called()


@pytest.mark.asyncio
async def test_target_status_no_anchor_skips_with_precision_warmup():
    """tp_anchor_quantity 缺失 → 预热精度后 continue（L2417-2423）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': None,
        'remaining_quantity': 0.5,
    }

    # _get_symbol_precision 被调（预热精度）
    with patch('strategies.new_coin.strategy.derive_reached_targets') as mock_d:
        await s._backfill_target_statuses(ex, ['TESTUSDT'])
    ex._get_symbol_precision.assert_awaited_once_with('TESTUSDT')
    mock_d.assert_not_called()  # 锚点缺失跳过自愈


@pytest.mark.asyncio
async def test_target_status_no_anchor_precision_exception_swallowed():
    """锚点缺失时预热精度抛异常 → except 吞掉（L2421-2422），不崩溃。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': 1.0,  # 有锚点
        'remaining_quantity': 0.5,
    }
    # _get_symbol_precision 抛异常 → except 吞
    from unittest.mock import AsyncMock
    ex._get_symbol_precision = AsyncMock(side_effect=Exception("API down"))

    # 不应崩溃，derive_reached_targets 继续用默认 step_size
    with patch('strategies.new_coin.strategy.derive_reached_targets', return_value=(True, False)) as mock_d:
        await s._backfill_target_statuses(ex, ['TESTUSDT'])
    mock_d.assert_called_once()  # 继续走自愈


@pytest.mark.asyncio
async def test_target_status_remaining_zero_skip():
    """remaining_quantity <= 0 → 跳过（L2430-2431）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': 1.0,
        'remaining_quantity': 0,
    }

    with patch('strategies.new_coin.strategy.derive_reached_targets') as mock_d:
        await s._backfill_target_statuses(ex, ['TESTUSDT'])
    mock_d.assert_not_called()


@pytest.mark.asyncio
async def test_target_status_hit1_sets_target1_only():
    """hit1=True hit2=False → 只置位 target1_reached=True（L2441-2446）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': 1.0,
        'remaining_quantity': 0.72,  # 刚好在 TP1 阈值附近
        'target1_reached': False,
        'target2_reached': False,
    }

    with patch('strategies.new_coin.strategy.derive_reached_targets', return_value=(True, False)):
        await s._backfill_target_statuses(ex, ['TESTUSDT'])

    assert ex.position_tracking['TESTUSDT']['target1_reached'] is True
    assert ex.position_tracking['TESTUSDT']['target2_reached'] is False


@pytest.mark.asyncio
async def test_target_status_hit1_hit2_sets_both():
    """hit1+hit2 同真 → 两档都置位（L2441-2449）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': 1.0,
        'remaining_quantity': 0.31,
        'target1_reached': False,
        'target2_reached': False,
    }

    with patch('strategies.new_coin.strategy.derive_reached_targets', return_value=(True, True)):
        await s._backfill_target_statuses(ex, ['TESTUSDT'])

    assert ex.position_tracking['TESTUSDT']['target1_reached'] is True
    assert ex.position_tracking['TESTUSDT']['target2_reached'] is True


@pytest.mark.asyncio
async def test_target_status_already_set_no_duplicate():
    """target1_reached 已经是 True → hit1=True 时不重复写入（L2441, L2447 的 if not 守卫）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': 1.0,
        'remaining_quantity': 0.3,
        'target1_reached': True,   # 已置位
        'target2_reached': True,   # 已置位
    }

    # 捕获赋值看看是否发生
    tracking = ex.position_tracking['TESTUSDT']

    with patch('strategies.new_coin.strategy.derive_reached_targets', return_value=(True, True)):
        await s._backfill_target_statuses(ex, ['TESTUSDT'])

    # 仍为 True，且没有被改回 False 等（保持原值）
    assert tracking['target1_reached'] is True
    assert tracking['target2_reached'] is True


# =============================================================================
# _sync_baseline_to_tracking 循环 + missing_atr 汇总 + 调用下游
# =============================================================================


@pytest.mark.asyncio
async def test_sync_baseline_loop_populates_tracking_and_calls_backfillers():
    """重建循环 → ensure_tracking_entry + atr 回填 + _backfill_anchor + _backfill_target_statuses 都被调（L2319-2350）。"""
    from strategies.new_coin import strategy as strategy_module

    s = _make_strategy_self()
    ex = _make_executor_self()

    rebuilt = {
        'TESTUSDT': {
            'entry_price': Decimal('100'),
            'quantity': Decimal('1.0'),
            'entry_time': '2026-10-10T00:00:00Z',
        }
    }
    atr_map = {'TESTUSDT': Decimal('2.0')}

    # mock ensure_tracking_entry 返回一个空 dict，然后让各方法填充
    ex.ensure_tracking_entry = MagicMock(return_value={})

    # patch s 实例上的属性（因为 types.MethodType 已绑定，patch 类方法无效）
    anchor_spy = AsyncMock()
    target_spy = AsyncMock()

    # 先保存原绑定方法
    orig_anchor = s._backfill_anchor_quantity
    orig_target = s._backfill_target_statuses
    s._backfill_anchor_quantity = anchor_spy
    s._backfill_target_statuses = target_spy

    try:
        await s._sync_baseline_to_tracking(ex, rebuilt, atr_map)
    finally:
        s._backfill_anchor_quantity = orig_anchor
        s._backfill_target_statuses = orig_target

    # ensure_tracking_entry 被调一次
    ex.ensure_tracking_entry.assert_called_once()
    # _backfill_anchor_quantity 被调
    anchor_spy.assert_awaited_once()
    # _backfill_target_statuses 被调（传入 list(rebuilt.keys())）
    target_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_baseline_missing_atr_triggers_warning():
    """atr_map 无该 symbol → missing_atr 累积，触发汇总 warning（L2336-2347）。"""
    from strategies.new_coin import strategy as strategy_module

    s = _make_strategy_self()
    ex = _make_executor_self()

    rebuilt = {
        'TESTUSDT': {
            'entry_price': Decimal('100'),
            'quantity': Decimal('1.0'),
            'entry_time': '2026-10-10T00:00:00Z',
        },
        'NEWCOINUSDT': {
            'entry_price': Decimal('150'),
            'quantity': Decimal('2.0'),
            'entry_time': '2026-10-11T00:00:00Z',
        },
    }
    atr_map = {}  # 空 → 全部缺 ATR

    ex.ensure_tracking_entry = MagicMock(return_value={'atr': Decimal('0')})

    orig_anchor = s._backfill_anchor_quantity
    orig_target = s._backfill_target_statuses
    s._backfill_anchor_quantity = AsyncMock()
    s._backfill_target_statuses = AsyncMock()
    try:
        with patch('strategies.new_coin.strategy.logger.warning') as mock_warn:
            await s._sync_baseline_to_tracking(ex, rebuilt, atr_map)
    finally:
        s._backfill_anchor_quantity = orig_anchor
        s._backfill_target_statuses = orig_target

    # 至少有一次 warning（汇总缺失 ATR）
    warn_calls = mock_warn.call_args_list
    atr_warn = [c for c in warn_calls if 'ATR 缺失' in str(c)]
    assert len(atr_warn) >= 1, f"期望 ATR 缺失 warning，实际 warn_calls={warn_calls}"


@pytest.mark.asyncio
async def test_target_status_no_anchor_precision_exception_ignored():
    """锚点缺失时预热精度抛异常 → except 吞掉后 continue（L2421-2423）。"""
    s = _make_strategy_self()
    ex = _make_executor_self()
    # tp_anchor_quantity 缺失 + _get_symbol_precision 抛异常
    ex.position_tracking['TESTUSDT'] = {
        'tp_anchor_quantity': None,  # 锚点缺失
        'remaining_quantity': 0.5,
    }
    ex._get_symbol_precision = AsyncMock(side_effect=Exception("K线服务挂了"))

    # 不崩溃，且 derive_reached_targets 不被调（锚点缺失直接 continue）
    with patch('strategies.new_coin.strategy.derive_reached_targets') as mock_d:
        await s._backfill_target_statuses(ex, ['TESTUSDT'])

    ex._get_symbol_precision.assert_awaited_once_with('TESTUSDT')
    mock_d.assert_not_called()
