"""B 段测试返工第二轮：derive_reached_targets 纯函数全覆盖。

生产代码改动（git diff HEAD）：仅 executor.py L76-103（28 行）。
strategy.py 无改动（还原到 HEAD）。
"""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock, patch


# ===================================================================
# executor.py：derive_reached_targets（纯函数，§3.2 档位阈值判定）
# ===================================================================


class TestDeriveReachedTargetsPure:
    """TP 档位判定——阈值公式全覆盖。"""

    def test_cross_tier_single_poll_levels_both(self):
        """AC-A1：同轮 exchange_qty 掉到 30% → (True, True)。"""
        from strategies.new_coin.executor import derive_reached_targets
        hit1, hit2 = derive_reached_targets(
            anchor_qty=1.0, exchange_qty=0.3,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
            tolerance_ratio=0.01, tolerance_absolute=0.0001, step_size=0.01,
        )
        assert hit1 is True
        assert hit2 is True

    def test_stepwise_two_levels_no_backfill(self):
        """AC-A2：剩 70% → TP1 命中、TP2 未命中。"""
        from strategies.new_coin.executor import derive_reached_targets
        hit1, hit2 = derive_reached_targets(
            anchor_qty=1.0, exchange_qty=0.7,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
            tolerance_ratio=0.01, tolerance_absolute=0.0001, step_size=0.01,
        )
        assert hit1 is True
        assert hit2 is False

    def test_tolerance_no_false_hit(self):
        """AC-A3：TP1/TP2 之间超容差 → 不报 TP2 命中。"""
        from strategies.new_coin.executor import derive_reached_targets
        hit1, hit2 = derive_reached_targets(
            anchor_qty=1.0, exchange_qty=0.7002,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
            tolerance_ratio=0.01, tolerance_absolute=0.0001, step_size=0.01,
        )
        assert hit1 is True
        assert hit2 is False

    def test_anchor_scales_not_hardcoded(self):
        """AC-A4：锚点缩放 → 阈值同比例缩放（非硬编码）。"""
        from strategies.new_coin.executor import derive_reached_targets
        hit1_10, _ = derive_reached_targets(
            anchor_qty=10.0, exchange_qty=7.0,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
            tolerance_ratio=0.01, tolerance_absolute=0.0001, step_size=0.1,
        )
        hit1_1, _ = derive_reached_targets(
            anchor_qty=1.0, exchange_qty=0.7,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
            tolerance_ratio=0.01, tolerance_absolute=0.0001, step_size=0.01,
        )
        assert hit1_10 is True
        assert hit1_1 is True

    def test_zero_qty_fails_closed(self):
        """fail-closed：exchange_qty <= 0 → (False, False)。"""
        from strategies.new_coin.executor import derive_reached_targets
        hit1, hit2 = derive_reached_targets(
            anchor_qty=1.0, exchange_qty=0.0,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
            tolerance_ratio=0.01, tolerance_absolute=0.0001, step_size=0.01,
        )
        assert hit1 is False
        assert hit2 is False

    def test_anchor_zero_fails_closed(self):
        """anchor_qty <= 0 → fail-closed (False, False)。"""
        from strategies.new_coin.executor import derive_reached_targets
        hit1, hit2 = derive_reached_targets(
            anchor_qty=0.0, exchange_qty=0.5,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
        )
        assert hit1 is False
        assert hit2 is False

    def test_exchange_negative_fails_closed(self):
        """exchange_qty < 0 → fail-closed。"""
        from strategies.new_coin.executor import derive_reached_targets
        hit1, hit2 = derive_reached_targets(
            anchor_qty=1.0, exchange_qty=-0.1,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
        )
        assert hit1 is False
        assert hit2 is False

    def test_partial_hit_between_thresholds(self):
        """TP1 命中但 exchange 尚未低到 TP2 阈值 → 中间边界。"""
        from strategies.new_coin.executor import derive_reached_targets
        # anchor=1.0, exchange=0.65, tp1=0.30(t=0.707), tp2=0.40(t=0.606)
        # 0.65 <= 0.707 → hit1=True; 0.65 > 0.606 → hit2=False
        hit1, hit2 = derive_reached_targets(
            anchor_qty=1.0, exchange_qty=0.65,
            target1_close_percent=Decimal('0.30'),
            target2_close_percent=Decimal('0.40'),
            tolerance_ratio=0.01, tolerance_absolute=0.0001,
        )
        assert hit1 is True
        assert hit2 is False


# ===================================================================
# 命令行快速运行
# ===================================================================
if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v', '-x']))
