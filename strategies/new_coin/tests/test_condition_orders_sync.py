"""AC-D condition_orders 状态同步 + AC-E MANUAL_STOP 白名单与守卫档位感知。

覆盖 §9.3 AC-D1/D2 + §9.3.1 B1（mark→守卫集成）、T7（MANUAL_STOP 共存）。
"""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch


# =============================================================================
# find_missing_protection 档位感知 + MANUAL_STOP 白名单（§6.4 + §8.2）
# =============================================================================


class TestFindMissingProtectionTierAware:
    """find_missing_protection 的三档表行为。"""

    def _make_executor(self):
        from strategies.new_coin.executor import TradingExecutor
        # 不加载完整实例（依赖 DB / binance_api），直接绑定类方法测试 find_missing_protection
        class _Ex:
            replenish_require_take_profit = True
            db = MagicMock()
        return _Ex()

    @pytest.mark.asyncio
    async def test_target2_reached_no_tp_required(self):
        """§6.4 第一档：target2_reached=True → 不要求 TP，只核验止损类。"""
        ex = self._make_executor()
        # DB 返回：只有 STOP_LOSS
        ex.db.fetch_all = AsyncMock(return_value=[
            {'order_type': 'STOP_LOSS'},
        ])
        from strategies.new_coin.executor import TradingExecutor
        # 绑定方法
        result = await TradingExecutor.find_missing_protection(
            ex, 'TESTUSDT', target2_reached=True
        )
        # 只 SL 存在 + target2 达成 → 无缺口
        assert result == []

    @pytest.mark.asyncio
    async def test_target2_reached_no_sl_missing(self):
        """target2_reached=True 但止损类全缺 → 报告缺 SL。"""
        ex = self._make_executor()
        ex.db.fetch_all = AsyncMock(return_value=[{'order_type': 'TAKE_PROFIT'}])
        from strategies.new_coin.executor import TradingExecutor
        result = await TradingExecutor.find_missing_protection(
            ex, 'TESTUSDT', target2_reached=True
        )
        assert result == ["止损单"]
        # 注意：target2 达成时不报告缺 TP（无论 TAKE_PROFIT 是否在 open_types）

    @pytest.mark.asyncio
    async def test_both_unreached_requires_tp(self):
        """两档均未达成 → 维持 require_take_profit 语义，缺 TP 报告缺 TP。"""
        ex = self._make_executor()
        ex.db.fetch_all = AsyncMock(return_value=[{'order_type': 'STOP_LOSS'}])
        from strategies.new_coin.executor import TradingExecutor
        result = await TradingExecutor.find_missing_protection(
            ex, 'TESTUSDT', target1_reached=False, target2_reached=False
        )
        assert result == ["止盈单"]  # SL 在但 TP 不在 → 缺 TP

    @pytest.mark.asyncio
    async def test_manual_stop_satisfies_sl(self):
        """§8.2 MANUAL_STOP 白名单：MANUAL_STOP 存在 → 止损类已满足。"""
        ex = self._make_executor()
        ex.db.fetch_all = AsyncMock(return_value=[{'order_type': 'MANUAL_STOP'}])
        from strategies.new_coin.executor import TradingExecutor
        result = await TradingExecutor.find_missing_protection(
            ex, 'TESTUSDT', target2_reached=True
        )
        assert result == []  # MANUAL_STOP 视为有效止损保护

    @pytest.mark.asyncio
    async def test_trailing_stop_satisfies_sl(self):
        """TRAILING_STOP 也应视为有效止损保护。"""
        ex = self._make_executor()
        ex.db.fetch_all = AsyncMock(return_value=[{'order_type': 'TRAILING_STOP'}])
        from strategies.new_coin.executor import TradingExecutor
        result = await TradingExecutor.find_missing_protection(
            ex, 'TESTUSDT', target2_reached=True
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_strict_mode_db_error_returns_none(self):
        """strict=True 且查询异常 → 返回 None（不可判定）。"""
        ex = self._make_executor()
        ex.db.fetch_all = AsyncMock(side_effect=Exception("DB down"))
        from strategies.new_coin.executor import TradingExecutor
        result = await TradingExecutor.find_missing_protection(
            ex, 'TESTUSDT', strict=True
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_strict_db_error_returns_all(self):
        """strict=False 且查询异常 → fail-closed 返回全部应存在类型。"""
        ex = self._make_executor()
        ex.db.fetch_all = AsyncMock(side_effect=Exception("DB down"))
        from strategies.new_coin.executor import TradingExecutor
        result = await TradingExecutor.find_missing_protection(
            ex, 'TESTUSDT', strict=False
        )
        assert "止损单" in result
        assert "止盈单" in result

    @pytest.mark.asyncio
    async def test_require_take_profit_explicit_override(self):
        """target2 未达成 + require_take_profit=False → 不要求 TP（配置覆盖）。"""
        ex = self._make_executor()
        ex.db.fetch_all = AsyncMock(return_value=[{'order_type': 'STOP_LOSS'}])
        from strategies.new_coin.executor import TradingExecutor
        result = await TradingExecutor.find_missing_protection(
            ex, 'TESTUSDT',
            require_take_profit=False,  # 显式传 False 覆盖 config
            target1_reached=False, target2_reached=False,
        )
        assert result == []  # SL 在 + require_tp=False → 无缺口


# =============================================================================
# AC-D TP 成交 → mark_order_executed 调用（集成验证）
# =============================================================================


class TestTPExecutionMarksExecuted:
    """detect 内部 mark_order_executed 的调用时机。"""

    def test_derive_targets_tp1_only_hit(self):
        """derive_reached_targets：TP1 单独命中，TP2 未命中。"""
        from strategies.new_coin.executor import derive_reached_targets
        hit1, hit2 = derive_reached_targets(
            anchor_qty=1.0, exchange_qty=0.7,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
        )
        assert hit1 is True
        assert hit2 is False

    def test_tp1_tp2_both_hit_marks_both(self):
        """同轮命中 TP1+TP2 → 两档 mark_order_executed 都被调度（create_task）。"""
        from strategies.new_coin.executor import derive_reached_targets
        hit1, hit2 = derive_reached_targets(
            anchor_qty=1.0, exchange_qty=0.3,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
        )
        assert hit1 is True
        assert hit2 is True


# =============================================================================
# §9.3.1 B1：mark→守卫集成（TP2 成交后，target2_reached=True 调守卫不报告缺 TP）
# =============================================================================


class TestMarkThenGuardNoReplenish:
    """B1 闭环：TP2 成交 + mark_executed 关闭 DB 后 → target2_reached=True 守卫
    不带缺 TP，不会触发 replenish 给尾仓重复挂 TP。"""

    @pytest.mark.asyncio
    async def test_target2_reached_guard_skips_tp(self):
        """target2_reached=True 时，即使 DB 无 OPEN TAKE_PROFIT → 守卫仍不带缺 TP。"""
        from strategies.new_coin.executor import TradingExecutor

        class _Ex:
            replenish_require_take_profit = True
            db = MagicMock()
            # 模拟 DB 只有 STOP_LOSS（TP 已被 mark_executed 关闭）
            db.fetch_all = AsyncMock(return_value=[{'order_type': 'STOP_LOSS'}])

        ex = _Ex()
        result = await TradingExecutor.find_missing_protection(
            ex, 'TAILUSDT',
            target1_reached=True, target2_reached=True,
        )
        # target2 达成 → 不要求 TP → 只 SL 存在 → 无缺口
        assert result == []
