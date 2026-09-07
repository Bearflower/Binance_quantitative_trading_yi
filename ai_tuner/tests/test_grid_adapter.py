"""
网格策略适配器单元测试
测试核心算法：ATR计算、市场状态估计、回测结果转换模拟指标
"""
import pytest
from datetime import datetime, timedelta

from ai_tuner.adapters.grid_adapter import GridAdapter
from ai_tuner.backtest.models import GridParams


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

    @staticmethod
    def _make_adapter() -> GridAdapter:
        """创建不经过 __init__ 的 GridAdapter 实例

        _backtest_result_to_simulation 是实例方法但不引用 self 的任何属性，
        因此通过 __new__ 跳过 __init__，避免依赖 db_manager 等外部资源。
        """
        return GridAdapter.__new__(GridAdapter)

    def test_backtest_result_to_simulation_basic(self):
        """应正确将回测结果转换为模拟指标"""
        adapter = self._make_adapter()
        # 场景参数：6格、2.5倍ATR间距
        scenario_params = GridParams(
            base_grid_count=6,
            grid_spacing_atr_multiplier=2.5,
            atr=25.0,
            current_price=1800.0,
        )
        market_stats = {"atr": 25.0, "current_price": 1800.0, "market_state": "震荡市场"}
        result = {"symbol": "ETHUSDT", "fill_count": 10, "total_pnl": 50.0}

        sim = adapter._backtest_result_to_simulation(
            result, "当前配置", market_stats, scenario_params
        )

        # 间距 = ATR * 倍数 = 25 * 2.5 = 62.5
        assert sim.scenario_name == "当前配置"
        assert sim.symbol == "ETHUSDT"
        assert sim.market_state == "震荡市场"
        assert sim.grid_count == 6
        assert sim.grid_spacing == 62.5
        assert sim.price_range_low < sim.price_range_high
        assert sim.profit_rate_per_fill > 0
        assert sim.estimated_fills_weekly == 10
        assert sim.estimated_profit_weekly == 50.0

    def test_backtest_result_to_simulation_scenario_differentiation(self):
        """三个不同场景参数应产生不同的模拟指标（验证bug修复核心）"""
        adapter = self._make_adapter()
        market_stats = {"atr": 25.0, "current_price": 1800.0, "market_state": "震荡市场"}
        result = {"symbol": "ETHUSDT", "fill_count": 10, "total_pnl": 50.0}

        # 当前 / 更密集 / 更稀疏 三种场景参数
        current_params = GridParams(base_grid_count=6, grid_spacing_atr_multiplier=2.5)
        dense_params = GridParams(base_grid_count=8, grid_spacing_atr_multiplier=2.125)
        sparse_params = GridParams(base_grid_count=4, grid_spacing_atr_multiplier=2.875)

        sim_current = adapter._backtest_result_to_simulation(
            result, "当前配置", market_stats, current_params
        )
        sim_dense = adapter._backtest_result_to_simulation(
            result, "更密集网格", market_stats, dense_params
        )
        sim_sparse = adapter._backtest_result_to_simulation(
            result, "更稀疏网格", market_stats, sparse_params
        )

        # 网格数各不相同：6 / 8 / 4
        grid_counts = {sim_current.grid_count, sim_dense.grid_count, sim_sparse.grid_count}
        assert grid_counts == {6, 8, 4}
        # 网格间距各不相同：62.5 / 53.125 / 71.875
        grid_spacings = {sim_current.grid_spacing, sim_dense.grid_spacing, sim_sparse.grid_spacing}
        assert grid_spacings == {62.5, 53.125, 71.875}
        # 价格区间下限/上限各不相同
        assert len({sim_current.price_range_low, sim_dense.price_range_low, sim_sparse.price_range_low}) == 3
        assert len({sim_current.price_range_high, sim_dense.price_range_high, sim_sparse.price_range_high}) == 3
        # 每格利润率各不相同
        assert len({sim_current.profit_rate_per_fill, sim_dense.profit_rate_per_fill, sim_sparse.profit_rate_per_fill}) == 3

    def test_backtest_result_to_simulation_zero_price(self):
        """当前价格为0时每格利润率应为0（边界条件）"""
        adapter = self._make_adapter()
        scenario_params = GridParams(base_grid_count=6, grid_spacing_atr_multiplier=2.5)
        market_stats = {"atr": 25.0, "current_price": 0.0, "market_state": "震荡市场"}
        result = {"symbol": "ETHUSDT", "fill_count": 0, "total_pnl": 0.0}

        sim = adapter._backtest_result_to_simulation(
            result, "零价格测试", market_stats, scenario_params
        )

        assert sim.profit_rate_per_fill == 0

    def test_backtest_result_to_simulation_zero_atr(self):
        """ATR为0时网格间距应为0、价格区间塌缩为当前价"""
        adapter = self._make_adapter()
        scenario_params = GridParams(base_grid_count=6, grid_spacing_atr_multiplier=2.5)
        market_stats = {"atr": 0.0, "current_price": 1800.0, "market_state": "震荡市场"}
        result = {"symbol": "ETHUSDT", "fill_count": 0, "total_pnl": 0.0}

        sim = adapter._backtest_result_to_simulation(
            result, "零ATR测试", market_stats, scenario_params
        )

        assert sim.grid_spacing == 0
        assert sim.price_range_low == 1800.0
        assert sim.price_range_high == 1800.0
        assert sim.profit_rate_per_fill == 0