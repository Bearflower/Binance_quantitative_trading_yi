"""AC-A 止盈成交检测：detect_take_profit_fills 重构 + derive_reached_targets 纯函数。

覆盖 §9.3 AC-A1~A8 回归用例 + §9.3.1 B2/B3/B5 闭环用例。
"""

import asyncio
import pytest
from decimal import Decimal
from typing import Optional
from unittest.mock import MagicMock, AsyncMock, patch


# =============================================================================
# 测试辅助：构造一个轻量 executor mock（只含 detect 所需属性）
# =============================================================================

def _make_minimal_executor():
    """构造只含 detect_take_profit_fills 及其依赖属性的最小 executor。

    避免加载完整 executor（依赖 DB / binance_api / notification 等），
    保持测试轻量、隔离、纯逻辑驱动。
    """
    from strategies.new_coin import executor as executor_module

    class _Minimal:
        # --- 配置属性（与 __init__ 一致，来自 config.yaml 默认值） ---
        position_detection_enabled = True
        qty_tolerance_ratio = 0.01
        qty_tolerance_absolute = 0.0001
        zero_qty_threshold = 0.0001
        target1_close_percent = Decimal('0.30')
        target2_close_percent = Decimal('0.40')
        stop_loss_percent = Decimal('0.05')
        default_step_size = Decimal('0.001')
        _zombie_confirm_cycles = 3

        # --- 状态容器 ---
        position_tracking: dict = {}
        _last_tracked_qty: dict = {}
        _unknown_reduction_counts: dict = {}
        _precision_cache: dict = {}
        # mock db（await mark_*）
        db = MagicMock()
        db.execute = AsyncMock()
        # mock binance_api
        binance_api = MagicMock()

        # --- 方法绑定到模块实现 ---
        detect_take_profit_fills = executor_module.TradingExecutor.detect_take_profit_fills
        _resolve_detection_baseline = executor_module.TradingExecutor._resolve_detection_baseline
        _apply_tp_marks = executor_module.TradingExecutor._apply_tp_marks
        update_target_status = executor_module.TradingExecutor.update_target_status
        _step_size_for = executor_module.TradingExecutor._step_size_for
        _resolve_tier_anchor = executor_module.TradingExecutor._resolve_tier_anchor
        _classify_fill_source = executor_module.TradingExecutor._classify_fill_source
        _handle_non_tp_reduction = executor_module.TradingExecutor._handle_non_tp_reduction

    return _Minimal()


def _make_tracking(
    entry_quantity: float = 1.0,
    tp_anchor_quantity: Optional[float] = None,
    target1_reached: bool = False,
    target2_reached: bool = False,
    entry_price: float = 100.0,
):
    """构造最小 tracking 条目（与 _build_tracking_entry 字段一致）。"""
    return {
        'entry_price': entry_price,
        'entry_quantity': entry_quantity,
        'tp_anchor_quantity': tp_anchor_quantity if tp_anchor_quantity is not None else entry_quantity,
        'target1_reached': target1_reached,
        'target2_reached': target2_reached,
        'remaining_quantity': entry_quantity,
        'algo_ids': {},
    }


def _prime_last_qty(ex, symbol: str, qty: float):
    """预置 _last_tracked_qty，模拟「上轮已记录过数量」。"""
    ex._last_tracked_qty[symbol] = qty


# =============================================================================
# derive_reached_targets 纯函数（§3.2）
# =============================================================================


class TestDeriveReachedTargets:
    """纯函数核心判定逻辑。"""

    def test_b2_step_size_truncation_no_miss(self):
        """§9.3.1 B2 截断边界：anchor=1.05、step=0.1、exchange=0.35。

        ROUND_DOWN 截断后剩余只增不减，2×step 截断项纳入容差后应命中 (True, True)。
        原公式无 step_size 时 tol2=0.03105+0.0001=0.03115 < 0.05 → 漏检。
        """
        from strategies.new_coin.executor import derive_reached_targets
        hit1, hit2 = derive_reached_targets(
            anchor_qty=1.05, exchange_qty=0.35,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
            tolerance_ratio=0.01, tolerance_absolute=0.0001, step_size=0.1,
        )
        assert hit1 is True
        assert hit2 is True

    def test_fail_closed_zero_anchor(self):
        """anchor_qty <= 0 → fail-closed。"""
        from strategies.new_coin.executor import derive_reached_targets
        h1, h2 = derive_reached_targets(
            anchor_qty=0, exchange_qty=0.3,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
        )
        assert (h1, h2) == (False, False)

    def test_fail_closed_negative_exchange(self):
        """exchange_qty <= 0 → fail-closed。"""
        from strategies.new_coin.executor import derive_reached_targets
        h1, h2 = derive_reached_targets(
            anchor_qty=1.0, exchange_qty=-0.1,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
        )
        assert (h1, h2) == (False, False)


# =============================================================================
# detect_take_profit_fills 状态机（§3.3）
# =============================================================================


class TestDetectTakeProfitFills:
    """detect_take_profit_fills 核心状态机回归。"""

    def test_cross_tier_single_poll_levels_both(self):
        """AC-A1：单轮 100%→30%（跨 TP1+TP2）→ 同时置位两档。

        原 BUG：先更新 last 再逐级 return → 同轮只置 target1，target2 永久漏置。
        修复后：先算 hit1/hit2 → 两个 if 顺序执行、互不 return → 同轮置位两档。
        """
        ex = _make_minimal_executor()
        tracking = _make_tracking(entry_quantity=1.0, tp_anchor_quantity=1.0)
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 1.0)

        result = ex.detect_take_profit_fills('TESTUSDT', 0.3, current_price=Decimal('95'))

        assert result == 2
        assert tracking['target1_reached'] is True
        assert tracking['target2_reached'] is True
        # 先置位、后更新 last 的关键修复点：_last_tracked_qty 最终值为 exchange_qty
        assert ex._last_tracked_qty['TESTUSDT'] == 0.3

    def test_stepwise_two_levels_no_backfill(self):
        """AC-A2：分步推进不回归——100%→70% 仅置 TP1；下轮 70%→30% 才置 TP2。"""
        ex = _make_minimal_executor()
        tracking = _make_tracking(entry_quantity=1.0, tp_anchor_quantity=1.0)
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 1.0)

        # 第一轮：100% → 70%
        r1 = ex.detect_take_profit_fills('TESTUSDT', 0.7, current_price=Decimal('95'))
        assert r1 == 1
        assert tracking['target1_reached'] is True
        assert tracking['target2_reached'] is False

        # 第二轮：70% → 30%
        _prime_last_qty(ex, 'TESTUSDT', 0.7)
        r2 = ex.detect_take_profit_fills('TESTUSDT', 0.3, current_price=Decimal('95'))
        assert r2 == 2
        assert tracking['target1_reached'] is True  # 保持已置位
        assert tracking['target2_reached'] is True

    def test_tolerance_no_false_hit(self):
        """AC-A3：减少量落在容差内 → 不置位任何目标。"""
        ex = _make_minimal_executor()
        tracking = _make_tracking(entry_quantity=1.0, tp_anchor_quantity=1.0)
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 1.0)

        # 减少 0.005% → 落在 1% 容差内（tol=max(0.01, 0.0001)=0.01）
        r = ex.detect_take_profit_fills('TESTUSDT', 0.99995, current_price=Decimal('95'))
        assert r is None
        assert tracking['target1_reached'] is False
        assert tracking['target2_reached'] is False

    def test_multi_symbol_isolation(self):
        """AC-A5：symbol A 检测 TP 不影响 symbol B 状态。"""
        ex = _make_minimal_executor()
        t1 = _make_tracking(entry_quantity=1.0, tp_anchor_quantity=1.0)
        t2 = _make_tracking(entry_quantity=2.0, tp_anchor_quantity=2.0)
        ex.position_tracking['AAAUSDT'] = t1
        ex.position_tracking['BBBUSDT'] = t2
        _prime_last_qty(ex, 'AAAUSDT', 1.0)
        _prime_last_qty(ex, 'BBBUSDT', 2.0)

        ex.detect_take_profit_fills('AAAUSDT', 0.3, current_price=Decimal('95'))

        assert t1['target1_reached'] is True
        assert t1['target2_reached'] is True
        # BBB 不受影响
        assert t2['target1_reached'] is False
        assert t2['target2_reached'] is False
        assert 'BBBUSDT' not in ex._unknown_reduction_counts

    def test_zero_qty_full_close(self):
        """AC-A6：exchange_qty < zero_qty_threshold → 返回 0。"""
        ex = _make_minimal_executor()
        tracking = _make_tracking(entry_quantity=1.0, tp_anchor_quantity=1.0)
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 1.0)

        r = ex.detect_take_profit_fills('TESTUSDT', 0.00005, current_price=Decimal('95'))
        assert r == 0

    def test_idempotent_no_repeat_log(self):
        """AC-A7：target1/2 均已置位后，后续减仓不重复置位。"""
        ex = _make_minimal_executor()
        tracking = _make_tracking(
            entry_quantity=1.0, tp_anchor_quantity=1.0,
            target1_reached=True, target2_reached=True,
        )
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 0.3)

        # 后续小减仓（如移动止损触发），已置位两档 → 返回 None
        r = ex.detect_take_profit_fills('TESTUSDT', 0.25, current_price=Decimal('95'))
        assert r is None

    def test_first_poll_records_qty(self):
        """首次跟踪（_last_tracked_qty 无此 symbol）→ 记录当前数量并返回 None。"""
        ex = _make_minimal_executor()
        tracking = _make_tracking(entry_quantity=1.0, tp_anchor_quantity=1.0)
        ex.position_tracking['TESTUSDT'] = tracking
        # 注意：ex._last_tracked_qty 此时为空（未预置）

        r = ex.detect_take_profit_fills('TESTUSDT', 1.0, current_price=Decimal('95'))
        assert r is None
        assert ex._last_tracked_qty['TESTUSDT'] == 1.0

    def test_tracking_missing_returns_none(self):
        """position_tracking 无此 symbol → 返回 None，不崩溃。"""
        ex = _make_minimal_executor()
        _prime_last_qty(ex, 'TESTUSDT', 1.0)

        r = ex.detect_take_profit_fills('TESTUSDT', 0.3, current_price=Decimal('95'))
        assert r is None

    def test_anchor_missing_fail_closed(self):
        """tp_anchor_quantity 缺失 → 退化 entry_quantity；entry 也缺失 → fail-closed。"""
        ex = _make_minimal_executor()
        tracking = _make_tracking(entry_quantity=0.0, tp_anchor_quantity=None)  # 锚点全缺失
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 1.0)

        r = ex.detect_take_profit_fills('TESTUSDT', 0.3, current_price=Decimal('95'))
        assert r is None
        # 锚点缺失时仍更新 last（避免下次永远卡在这里）
        assert ex._last_tracked_qty['TESTUSDT'] == 0.3


# =============================================================================
# B3/B5 闭环：减仓来源甄别
# =============================================================================


class TestFillSourceClassification:
    """_classify_fill_source + _handle_non_tp_reduction 回归。"""

    def test_tp_fill_with_price_rebound_still_sets_target(self):
        """§9.3.1 B5 正例：TP2 成交后价格反弹至中间地带（低于 SL 触发价）→ 仍判 TP。

        场景：entry=100, stop_loss_percent=0.05 → SL 触发价=105。
        TP2 成交后 detect 时 current_price=102（反弹但仍 < 105）→ 判 TP → 置位 target2。
        原 BUG（v1.1）：要求 price ≤ TP 目标价 → 反弹后判 UNKNOWN → 永久漏置。
        """
        ex = _make_minimal_executor()
        tracking = _make_tracking(
            entry_quantity=1.0, tp_anchor_quantity=1.0,
            entry_price=100.0,
        )
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 1.0)

        # exchange=0.3 → hit1=True, hit2=True；current_price=102（低于 SL 触发价 105）
        r = ex.detect_take_profit_fills('TESTUSDT', 0.3, current_price=Decimal('102'))
        assert r == 2
        assert tracking['target2_reached'] is True

    def test_tp_fill_mirror_sl_price_excludes_tp(self):
        """§9.3.1 B5 镜像负例：current_price ≥ SL 触发价 → 判 SL，不置位。"""
        ex = _make_minimal_executor()
        tracking = _make_tracking(
            entry_quantity=1.0, tp_anchor_quantity=1.0,
            entry_price=100.0,
        )
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 1.0)

        # exchange=0.3, current_price=106（≥ SL 触发价 105）→ 判 SL，不置位
        r = ex.detect_take_profit_fills('TESTUSDT', 0.3, current_price=Decimal('106'))
        # SL 分支会推进 last 但不置位
        assert tracking['target1_reached'] is False
        assert tracking['target2_reached'] is False
        # SL 分支确实推进了 last（表示减仓已收敛）
        assert ex._last_tracked_qty['TESTUSDT'] == 0.3

    def test_sl_partial_fill_no_tp_mark(self):
        """§9.3.1 B3：SL 部分成交 → 不置位 target、不 mark TP、剩余 OPEN TAKE_PROFIT 被 mark_open_orders_canceled。

        判据：exchange 减少但 current_price ≥ SL 触发价 → source="SL"。
        """
        ex = _make_minimal_executor()
        tracking = _make_tracking(
            entry_quantity=1.0, tp_anchor_quantity=1.0,
            entry_price=100.0,
        )
        tracking['algo_ids'] = {'tp1': 'ALGO_TP1_123', 'tp2': 'ALGO_TP2_456'}
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 0.6)  # 上轮剩 0.6（刚 TP1 后）
        # exchange 从 0.6 降到 0.55（SL 部分成交），但价格越过 SL 触发价
        # tol=max(0.6*0.01, 0.0001)=0.0061, exchange=0.55 < 0.6-0.0061 → 触发减仓检测

        with patch(
            'strategies.new_coin.executor.mark_open_orders_canceled',
            new=AsyncMock(),
        ) as mock_mark_open:
            # 触发 detect（SL 部分成交场景）
            r = ex.detect_take_profit_fills(
                'TESTUSDT', 0.55, current_price=Decimal('106')
            )

        assert r is None
        assert tracking['target1_reached'] is False
        assert tracking['target2_reached'] is False
        # SL 分支推进了 last
        assert ex._last_tracked_qty['TESTUSDT'] == 0.55
        # mark_open_orders_canceled 被调用（在异步 create_task 里，此处只是验证不崩）

    def test_unknown_reduction_keeps_last(self):
        """UNKNOWN 减仓不推进 _last_tracked_qty（方案乙要点，防永久漏检）。"""
        ex = _make_minimal_executor()
        tracking = _make_tracking(
            entry_quantity=1.0, tp_anchor_quantity=1.0,
            entry_price=100.0,
        )
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 0.8)

        # exchange 从 0.8 降到 0.79（减少但不跨档、价格也不在 SL 区）→ UNKNOWN
        # tol=max(0.8*0.01, 0.0001)=0.0081, exchange=0.79 >= 0.8-0.0081 → 容差内
        # 让它容差外：0.791 → exchange >= 0.7919 不触发。那就 0.78
        r = ex.detect_take_profit_fills('TESTUSDT', 0.78, current_price=Decimal('99'))
        # UNKNOWN → 不推进 last
        assert ex._last_tracked_qty['TESTUSDT'] == 0.8  # 原值未被推进
        # 重试计数 +1
        assert ex._unknown_reduction_counts['TESTUSDT'] == 1

    def test_unknown_reduction_forces_after_limit(self):
        """UNKNOWN 超重试上限 → 强制推进 last。"""
        ex = _make_minimal_executor()
        ex._zombie_confirm_cycles = 2  # 降低阈值便于测试
        tracking = _make_tracking(
            entry_quantity=1.0, tp_anchor_quantity=1.0,
            entry_price=100.0,
        )
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 0.8)
        # 先制造 2 次 UNKNOWN
        ex._unknown_reduction_counts['TESTUSDT'] = 2

        r = ex.detect_take_profit_fills('TESTUSDT', 0.78, current_price=Decimal('99'))
        # 第 3 次 > 2 → 强制推进
        assert ex._last_tracked_qty['TESTUSDT'] == 0.78

    def test_unknown_force_fallback_remaining_quantity(self):
        """UNKNOWN 超重试且 exchange_qty=None → 兜底用 tracking['remaining_quantity']。"""
        ex = _make_minimal_executor()
        ex._zombie_confirm_cycles = 2
        tracking = _make_tracking(
            entry_quantity=1.0, tp_anchor_quantity=1.0,
            entry_price=100.0,
        )
        tracking['remaining_quantity'] = 0.55
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 0.8)
        ex._unknown_reduction_counts['TESTUSDT'] = 2

        # 故意调 _handle_non_tp_reduction 传 exchange_qty=None
        ex._handle_non_tp_reduction('TESTUSDT', tracking, 'UNKNOWN', exchange_qty=None)
        # 强制推进 → 走 remaining_quantity 兜底
        assert ex._last_tracked_qty['TESTUSDT'] == 0.55


# =============================================================================
# _step_size_for / _resolve_tier_anchor
# =============================================================================


class TestHelperMethods:
    """辅助方法的契约验证。"""

    def test_step_size_for_cache_hit(self):
        """命中 _precision_cache → 直接返回 step_size，不触达交易所。"""
        ex = _make_minimal_executor()
        ex._precision_cache['XUSDT'] = (Decimal('0.01'), Decimal('0.001'))
        assert ex._step_size_for('XUSDT') == 0.001

    def test_step_size_for_cache_miss(self):
        """未命中 → 返回 default_step_size。"""
        ex = _make_minimal_executor()
        ex.default_step_size = Decimal('0.005')
        assert ex._step_size_for('YUSDT') == 0.005

    def test_resolve_tier_anchor_prefers_tp_anchor(self):
        """优先 tp_anchor_quantity。"""
        ex = _make_minimal_executor()
        t = _make_tracking(entry_quantity=2.0, tp_anchor_quantity=5.0)
        anchor = ex._resolve_tier_anchor('X', t)
        assert anchor == 5.0

    def test_resolve_tier_anchor_fallback_entry(self):
        """tp_anchor_quantity 缺失 → 退化 entry_quantity（并打 warning）。"""
        ex = _make_minimal_executor()
        t = _make_tracking(entry_quantity=3.0, tp_anchor_quantity=None)
        anchor = ex._resolve_tier_anchor('X', t)
        assert anchor == 3.0

    def test_resolve_tier_anchor_fail_closed(self):
        """全缺失 → 返回 None。"""
        ex = _make_minimal_executor()
        t = _make_tracking(entry_quantity=0.0, tp_anchor_quantity=None)
        anchor = ex._resolve_tier_anchor('X', t)
        assert anchor is None


# =============================================================================
# 边界条件覆盖（补齐 coverage Missing 的行）
# =============================================================================


class TestBoundaryCoverage:
    """覆盖核心函数的剩余边界分支。"""

    def test_position_detection_disabled_short_circuits(self):
        """position_detection_enabled=False → 直接返回 None，不更新 last。"""
        ex = _make_minimal_executor()
        ex.position_detection_enabled = False
        tracking = _make_tracking(entry_quantity=1.0, tp_anchor_quantity=1.0)
        ex.position_tracking['TESTUSDT'] = tracking
        _prime_last_qty(ex, 'TESTUSDT', 1.0)

        r = ex.detect_take_profit_fills('TESTUSDT', 0.3, current_price=Decimal('95'))
        assert r is None
        assert ex._last_tracked_qty['TESTUSDT'] == 1.0  # 未被更新

    def test_resolve_tier_anchor_degrades_with_warning(self):
        """tp_anchor_quantity=None 但 entry_quantity>0 → 退化并打 warning。"""
        ex = _make_minimal_executor()
        t = _make_tracking(entry_quantity=3.0, tp_anchor_quantity=None)
        # tp_anchor_quantity 设为 None 但 entry_quantity 有效
        t['tp_anchor_quantity'] = None
        anchor = ex._resolve_tier_anchor('X', t)
        assert anchor == 3.0

    def test_classify_fill_source_price_none(self):
        """current_price=None → 直接返回 UNKNOWN。"""
        ex = _make_minimal_executor()
        t = _make_tracking(entry_price=100.0)
        r = ex._classify_fill_source('X', t, current_price=None, tier_hit=True)
        assert r == "UNKNOWN"

    def test_classify_fill_source_entry_zero(self):
        """entry_price<=0 → UNKNOWN（无法计算 SL 触发价）。"""
        ex = _make_minimal_executor()
        t = _make_tracking(entry_price=0.0)
        r = ex._classify_fill_source('X', t, current_price=Decimal('90'), tier_hit=True)
        assert r == "UNKNOWN"


# =============================================================================
# 异步路径覆盖（pytest-asyncio 提供 running loop）
# =============================================================================


@pytest.mark.asyncio
async def test_sl_reduction_async_cancel_path():
    """有 running loop 时，SL 分支的 create_task 异步撤 TP 路径被触发。"""
    import asyncio
    from strategies.new_coin.executor import TradingExecutor

    ex = _make_minimal_executor()
    tracking = _make_tracking(
        entry_quantity=1.0, tp_anchor_quantity=1.0,
        entry_price=100.0,
    )
    tracking['algo_ids'] = {'tp1': 'TP1_ALGO', 'tp2': 'TP2_ALGO'}
    ex.position_tracking['TESTUSDT'] = tracking
    _prime_last_qty(ex, 'TESTUSDT', 0.6)
    ex.binance_api.cancel_algo_order = AsyncMock()
    ex.db.fetch_all = AsyncMock(return_value=[])
    ex.db.execute = AsyncMock()

    with patch(
        'strategies.new_coin.executor.mark_open_orders_canceled',
        new=AsyncMock(),
    ) as mock_mark:
        # SL 部分成交场景（价格越过 SL 触发价 105）
        r = ex.detect_take_profit_fills('TESTUSDT', 0.55, current_price=Decimal('106'))
        # 等一下让 create_task 完成
        await asyncio.sleep(0.05)

    # SL 分支推进了 last
    assert ex._last_tracked_qty['TESTUSDT'] == 0.55
    # 异步撤单被触发（至少调了 mark_open_orders_canceled 一次）
    assert mock_mark.called


@pytest.mark.asyncio
async def test_tp_fill_mark_order_executed_path():
    """有 running loop 时，TP 分支的 create_task(mark_order_executed) 被触发。"""
    import asyncio
    from strategies.new_coin.executor import TradingExecutor

    ex = _make_minimal_executor()
    tracking = _make_tracking(
        entry_quantity=1.0, tp_anchor_quantity=1.0,
        entry_price=100.0,
    )
    tracking['algo_ids'] = {'tp1': 'TP1_ALGO', 'tp2': 'TP2_ALGO'}
    ex.position_tracking['TESTUSDT'] = tracking
    _prime_last_qty(ex, 'TESTUSDT', 1.0)
    ex.db.execute = AsyncMock()

    with patch(
        'strategies.new_coin.executor.mark_order_executed',
        new=AsyncMock(),
    ) as mock_mark_exec:
        # 跨档同轮命中（会触发 TP1 + TP2 的 mark_order_executed）
        r = ex.detect_take_profit_fills('TESTUSDT', 0.3, current_price=Decimal('95'))
        await asyncio.sleep(0.05)

    assert tracking['target1_reached'] is True
    assert tracking['target2_reached'] is True
    # mark_order_executed 应该被调了两次（tp1 + tp2）
    assert mock_mark_exec.call_count == 2


@pytest.mark.asyncio
async def test_sl_reduction_cancel_algo_exception_path():
    """SL 分支异步撤单时 cancel_algo_order 抛异常 → except 路径被覆盖。"""
    import asyncio
    from strategies.new_coin.executor import TradingExecutor

    ex = _make_minimal_executor()
    tracking = _make_tracking(
        entry_quantity=1.0, tp_anchor_quantity=1.0,
        entry_price=100.0,
    )
    tracking['algo_ids'] = {'tp1': 'TP1_ALGO', 'tp2': 'TP2_ALGO'}
    ex.position_tracking['TESTUSDT'] = tracking
    _prime_last_qty(ex, 'TESTUSDT', 0.6)

    # 让 cancel_algo_order 抛异常
    from unittest.mock import AsyncMock
    ex.binance_api.cancel_algo_order = AsyncMock(
        side_effect=Exception("API down")
    )

    with patch(
        'strategies.new_coin.executor.mark_open_orders_canceled',
        new=AsyncMock(),
    ):
        r = ex.detect_take_profit_fills('TESTUSDT', 0.55, current_price=Decimal('106'))
        await asyncio.sleep(0.05)

    assert ex._last_tracked_qty['TESTUSDT'] == 0.55

@pytest.mark.asyncio
async def test_sl_mark_canceled_exception_handler():
    """SL 异步撤 TP 时 mark_open_orders_canceled 抛异常 → 内层 except 被覆盖。"""
    import asyncio
    from unittest.mock import AsyncMock, patch

    ex = _make_minimal_executor()
    tracking = _make_tracking(
        entry_quantity=1.0, tp_anchor_quantity=1.0,
        entry_price=100.0,
    )
    tracking['algo_ids'] = {'tp1': 'TP1_ALGO', 'tp2': 'TP2_ALGO'}
    ex.position_tracking['TESTUSDT'] = tracking
    _prime_last_qty(ex, 'TESTUSDT', 0.6)
    # cancel_algo_order 正常，mark_open_orders_canceled 抛异常 → 内层 except
    ex.binance_api.cancel_algo_order = AsyncMock()
    ex.db.execute = AsyncMock()

    with patch(
        'strategies.new_coin.executor.mark_open_orders_canceled',
        new=AsyncMock(side_effect=Exception("DB write failed")),
    ):
        r = ex.detect_take_profit_fills('TESTUSDT', 0.55, current_price=Decimal('106'))
        await asyncio.sleep(0.05)

    # 主流程不受影响
    assert ex._last_tracked_qty['TESTUSDT'] == 0.55

# =============================================================================
# AC-A §3.5.2：TradingExecutor.__init__ 新增字段（L330-333）
# =============================================================================


def test_executor_init_sets_reconcile_fields():
    """真实构造 TradingExecutor → _unknown_reduction_counts / _zombie_confirm_cycles 被正确初始化。

    Patch 掉 load_ownership_config / CapitalManager 等重型依赖，聚焦 __init__ 内
    trading_config.get('reconcile') 分支对 L330-333 的赋值路径。
    """
    from strategies.new_coin import executor as executor_module
    from unittest.mock import patch

    # 构造轻量 config，显式带 reconcile 段覆盖默认
    config = {
        'strategy': {'name': 'new_coin'},
        'trading': {
            'leverage': 5,
            'reconcile': {'zombie_confirm_cycles': 7},  # 覆盖默认 3
        },
    }

    with patch.object(executor_module, 'load_ownership_config', return_value={}):
        with patch.object(executor_module, 'CapitalManager'):
            with patch.object(executor_module, 'KLineService'):
                with patch.object(executor_module, 'BinanceAPIError', new=Exception):
                    ex = executor_module.TradingExecutor(
                        binance_api=MagicMock(),
                        db=MagicMock(),
                        notification=MagicMock(),
                        config=config,
                    )

    # L330: _unknown_reduction_counts 初始化为空字典
    assert hasattr(ex, '_unknown_reduction_counts'), "L330 字段应存在"
    assert isinstance(ex._unknown_reduction_counts, dict)
    assert ex._unknown_reduction_counts == {}
    # L333: zombie_confirm_cycles 从 reconcile 配置读取
    assert ex._zombie_confirm_cycles == 7, f"期望 7，实际 {ex._zombie_confirm_cycles}"


def test_executor_init_reconcile_defaults_when_missing():
    """reconcile 段缺失 → _zombie_confirm_cycles 回退默认 3。"""
    from strategies.new_coin import executor as executor_module

    config = {
        'strategy': {'name': 'new_coin'},
        'trading': {'leverage': 5},  # 无 reconcile 段
    }

    with patch.object(executor_module, 'load_ownership_config', return_value={}):
        with patch.object(executor_module, 'CapitalManager'):
            with patch.object(executor_module, 'KLineService'):
                ex = executor_module.TradingExecutor(
                    binance_api=MagicMock(),
                    db=MagicMock(),
                    notification=MagicMock(),
                    config=config,
                )

    assert ex._zombie_confirm_cycles == 3, f"期望默认 3，实际 {ex._zombie_confirm_cycles}"
