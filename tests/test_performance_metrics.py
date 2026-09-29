"""
绩效指标纯函数模块单测

覆盖 shared/performance_metrics.py 中的 annualized_sharpe 和 max_drawdown_ratio。
所有测试不依赖数据库、不依赖 Binance API，完全隔离。
"""
import math

import pytest

from shared.performance_metrics import (
    ANNUALIZATION_PERIODS,
    MIN_SAMPLE_COUNT,
    annualized_sharpe,
    max_drawdown_ratio,
)


class TestAnnualizedSharpe:
    """年化夏普比率测试"""

    def test_normal_case(self):
        """正常值：均值 0.001，标准差 0.01，sharpe ≈ 0.1 * sqrt(365) ≈ 1.91"""
        # 构造收益率序列：均值 0.001，标准差约 0.01
        # 用一组确定性数据：[0.011, -0.009, 0.011, -0.009, 0.011, -0.009, ...]
        # 均值 = 0.001，标准差 ≈ 0.01
        daily = [0.011, -0.009] * 100  # 200 个数据点
        result = annualized_sharpe(daily)
        assert result is not None
        assert abs(result - 0.1 * math.sqrt(ANNUALIZATION_PERIODS)) < 0.01

    def test_sample_insufficient_single(self):
        """样本不足（1 个数据点）→ None"""
        assert annualized_sharpe([0.01]) is None

    def test_sample_insufficient_empty(self):
        """空列表 → None"""
        assert annualized_sharpe([]) is None

    def test_std_zero_all_zeros(self):
        """全 0 收益率 → 标准差为 0 → None"""
        assert annualized_sharpe([0.0] * 30) is None

    def test_std_zero_all_same(self):
        """所有收益率相同（非零）→ 标准差为 0 → None"""
        assert annualized_sharpe([0.01] * 30) is None

    def test_consistent_negative(self):
        """持续亏损：均值为负 → sharpe 为负"""
        daily = [-0.011, 0.009] * 100  # 均值 = -0.001
        result = annualized_sharpe(daily)
        assert result is not None
        assert result < 0

    def test_two_samples(self):
        """刚好 MIN_SAMPLE_COUNT=2 个样本 → 应能计算"""
        result = annualized_sharpe([0.01, -0.01])
        assert result is not None


class TestMaxDrawdownRatio:
    """最大回撤比例测试"""

    def test_known_sequence(self):
        """净值 [1.0, 1.2, 1.1, 0.9, 1.0] → peak=1.2, 最低点=0.9, max_dd = (1.2-0.9)/1.2 = 0.25"""
        nv = [1.0, 1.2, 1.1, 0.9, 1.0]
        result = max_drawdown_ratio(nv)
        assert result is not None
        assert abs(result - 0.25) < 1e-9

    def test_monotonically_increasing(self):
        """单调递增 [1.0, 1.1, 1.2] → max_dd = 0.0（从未从高点回落）"""
        nv = [1.0, 1.1, 1.2]
        result = max_drawdown_ratio(nv)
        assert result == 0.0

    def test_all_flat(self):
        """全部相同 [1.0, 1.0, 1.0] → max_dd = 0.0"""
        assert max_drawdown_ratio([1.0, 1.0, 1.0]) == 0.0

    def test_sample_insufficient_single(self):
        """样本不足（1 个数据点）→ None"""
        assert max_drawdown_ratio([1.0]) is None

    def test_sample_insufficient_empty(self):
        """空列表 → None"""
        assert max_drawdown_ratio([]) is None

    def test_two_samples_drop(self):
        """刚好 MIN_SAMPLE_COUNT=2，且下跌 → max_dd > 0"""
        result = max_drawdown_ratio([1.0, 0.8])
        assert result is not None
        assert abs(result - 0.2) < 1e-9

    def test_two_samples_rise(self):
        """刚好 MIN_SAMPLE_COUNT=2，且上涨 → max_dd = 0"""
        assert max_drawdown_ratio([1.0, 1.2]) == 0.0

    def test_large_drawdown(self):
        """从 1.0 跌到 0.5 再反弹 → max_dd = (1.0-0.5)/1.0 = 0.5"""
        nv = [1.0, 0.8, 0.5, 0.6, 0.9, 1.1]
        result = max_drawdown_ratio(nv)
        assert abs(result - 0.5) < 1e-9


class TestConstants:
    """常量值验证"""

    def test_annualization_periods(self):
        """币圈 7x24 年化系数应为 365"""
        assert ANNUALIZATION_PERIODS == 365

    def test_min_sample_count(self):
        """最小样本数应为 2"""
        assert MIN_SAMPLE_COUNT == 2
