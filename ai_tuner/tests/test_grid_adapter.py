"""
网格策略适配器单元测试
测试核心算法：ATR计算、市场状态估计、场景模拟推演
"""
import pytest
from datetime import datetime, timedelta

from ai_tuner.adapters.grid_adapter import GridAdapter


class TestGridAdapterCore:
    """测试GridAdapter核心算法（不依赖数据库和网络）"""

    def test_calc_simple_atr_basic(self):
        """应正确计算简单ATR"""
        klines = []
        base_price = 1800.0
        for i in range(20):
            klines.append({
                "open": base_price + i * 2,
                "high": base_price + i * 2 + 10,
                "low": base_price + i * 2 - 8,
                "close": base_price + i * 2 + 2,
                "volume": 1000,
            })

        atr = GridAdapter._calc_simple_atr(klines, period=14)
        assert atr > 0, f"ATR 应大于0，实际为 {atr}"
        assert atr < 50, f"ATR 应在合理范围，实际为 {atr}"

    def test_calc_simple_atr_insufficient_data(self):
        """数据不足时应返回0"""
        klines = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}]
        atr = GridAdapter._calc_simple_atr(klines, period=14)
        assert atr == 0.0, f"数据不足时应返回0，实际为 {atr}"

    def test_calc_simple_atr_zero_movement(self):
        """价格无波动时ATR应为0"""
        klines = []
        for i in range(20):
            klines.append({
                "open": 100,
                "high": 100,
                "low": 100,
                "close": 100,
                "volume": 1000,
            })
        atr = GridAdapter._calc_simple_atr(klines, period=14)
        assert atr == 0.0, f"零波动时ATR应为0，实际为 {atr}"

    def test_estimate_market_state_strong_trend(self):
        """连续同向上涨应识别为强趋势"""
        klines = []
        price = 100.0
        for i in range(24):
            klines.append({
                "open": price,
                "high": price + 2,
                "low": price - 0.5,
                "close": price + 1.5,
                "volume": 1000,
            })
            price += 1.5

        atr = 3.0
        current_price = price
        state = GridAdapter._estimate_market_state(klines, atr, current_price)
        assert state == "强趋势", f"应识别为强趋势，实际为 {state}"

    def test_estimate_market_state_oscillation(self):
        """来回波动应识别为震荡市场"""
        klines = []
        price = 100.0
        for i in range(24):
            direction = 1 if i % 2 == 0 else -1
            klines.append({
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price + direction * 0.5,
                "volume": 1000,
            })
            price += direction * 0.5

        atr = 2.0
        current_price = price
        state = GridAdapter._estimate_market_state(klines, atr, current_price)
        assert state == "震荡市场", f"应识别为震荡市场，实际为 {state}"

    def test_simulate_scenario_basic(self):
        """模拟场景应返回合理估值"""
        scenario = {"name": "测试场景", "grid_count": 8, "spacing_mult": 2.0}
        result = GridAdapter._simulate_scenario(
            None,  # 静态方法，self不会被使用
            scenario=scenario,
            symbol="ETHUSDT",
            market_state="震荡市场",
            current_price=1800.0,
            atr=25.0,
            total_price_swing=500.0,
            leverage=10,
            margin=500,
            single_margin=100,
        )

        assert result.scenario_name == "测试场景"
        assert result.symbol == "ETHUSDT"
        assert result.grid_count == 8
        assert result.grid_spacing > 0
        assert result.price_range_low < result.price_range_high
        assert result.profit_rate_per_fill > 0
        assert result.estimated_fills_weekly >= 0
        assert 0 < result.confidence <= 1

    def test_simulate_scenario_dense_vs_sparse(self):
        """密集网格的填充数应高于稀疏网格"""
        base_params = dict(
            symbol="ETHUSDT",
            market_state="震荡市场",
            current_price=1800.0,
            atr=25.0,
            total_price_swing=500.0,
            leverage=10,
            margin=500,
            single_margin=100,
        )

        dense = GridAdapter._simulate_scenario(
            None,
            scenario={"name": "密集", "grid_count": 12, "spacing_mult": 1.7},
            **base_params,
        )
        sparse = GridAdapter._simulate_scenario(
            None,
            scenario={"name": "稀疏", "grid_count": 4, "spacing_mult": 2.3},
            **base_params,
        )

        assert dense.estimated_fills_weekly >= sparse.estimated_fills_weekly, \
            f"密集网格({dense.estimated_fills_weekly}次)填充应不少于稀疏网格({sparse.estimated_fills_weekly}次)"
        assert dense.grid_spacing <= sparse.grid_spacing, \
            f"密集网格间距({dense.grid_spacing})应小于稀疏网格({sparse.grid_spacing})"