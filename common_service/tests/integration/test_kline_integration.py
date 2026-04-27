"""集成测试 - 模拟 K 线数据服务"""

import pytest
import asyncio
import sys
from pathlib import Path
from datetime import datetime

# 添加路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from tests.integration.mock_data import MockKlineGenerator, create_mock_generators
from kline_data_service.models.kline import KlineData
from kline_data_service.core.indicator import TechnicalIndicatorCalculator


class TestMockDataGenerator:
    """测试模拟数据生成器"""

    def test_single_kline(self):
        """测试生成单条 K 线"""
        generator = MockKlineGenerator(base_price=50000.0)
        kline = generator.generate_kline(
            "BTCUSDT", "1h", int(datetime.now().timestamp() * 1000)
        )

        assert len(kline) == 12
        assert float(kline[1]) > 0  # 开盘价
        assert float(kline[4]) > 0  # 收盘价
        assert float(kline[2]) >= float(kline[4])  # 最高价 >= 收盘价
        assert float(kline[3]) <= float(kline[4])  # 最低价 <= 收盘价

        print(f"\n单条 K 线：开盘={kline[1]}, 收盘={kline[4]}")

    def test_multiple_klines(self):
        """测试生成多条 K 线"""
        generator = MockKlineGenerator(base_price=50000.0)
        klines = generator.generate_klines("BTCUSDT", "1h", count=100)

        assert len(klines) == 100
        assert all(len(k) == 12 for k in klines)

        # 检查价格连续性
        for i in range(1, len(klines)):
            prev_close = float(klines[i - 1][4])
            curr_open = float(klines[i][1])
            # 下一条的开盘价应该等于上一条的收盘价
            assert abs(prev_close - curr_open) < 0.0001

        print(f"\n生成 {len(klines)} 条 K 线，价格连续")

    def test_different_symbols(self):
        """测试不同币种"""
        generators = create_mock_generators()

        for symbol, gen in generators.items():
            klines = gen.generate_klines(symbol, "1h", count=10)
            assert len(klines) == 10

            # 检查价格范围
            prices = [float(k[4]) for k in klines]
            base_price = gen.base_price
            for price in prices:
                # 价格应该在基础价格的±10% 范围内
                assert base_price * 0.9 <= price <= base_price * 1.1

        print(f"\n测试 {len(generators)} 个币种生成器")

    def test_different_intervals(self):
        """测试不同周期"""
        generator = MockKlineGenerator(base_price=50000.0)
        intervals = ["15m", "1h", "4h", "1d"]

        for interval in intervals:
            klines = generator.generate_klines("BTCUSDT", interval, count=10)
            assert len(klines) == 10

            # 检查时间间隔
            for i in range(1, len(klines)):
                time_diff = klines[i][0] - klines[i - 1][0]
                expected_diff = generator._interval_to_milliseconds(interval)
                assert time_diff == expected_diff

        print(f"\n测试 {len(intervals)} 个周期")


class TestKlineDataProcessing:
    """测试 K 线数据处理"""

    def test_mock_to_kline_model(self):
        """测试模拟数据转换为 KlineData 模型"""
        generator = MockKlineGenerator(base_price=50000.0)
        kline_data = generator.generate_kline(
            "BTCUSDT", "1h", int(datetime.now().timestamp() * 1000)
        )

        kline = KlineData.from_binance_data("BTCUSDT", "1h", kline_data)

        assert kline.symbol == "BTCUSDT"
        assert kline.interval == "1h"
        assert kline.open_price > 0
        assert kline.close_price > 0
        assert kline.high_price >= kline.close_price
        assert kline.low_price <= kline.close_price
        assert kline.volume > 0

        print(f"\nK 线模型：{kline.symbol} 开盘={kline.open_price}, 收盘={kline.close_price}")

    def test_batch_to_dict(self):
        """测试批量转换为字典"""
        generator = MockKlineGenerator(base_price=50000.0)
        klines_raw = generator.generate_klines("BTCUSDT", "1h", count=10)

        dicts = []
        for data in klines_raw:
            kline = KlineData.from_binance_data("BTCUSDT", "1h", data)
            dicts.append(kline.to_dict())

        assert len(dicts) == 10
        assert all("symbol" in d for d in dicts)
        assert all("open_time" in d for d in dicts)
        assert all("close_price" in d for d in dicts)

        print(f"\n批量转换 {len(dicts)} 条数据")


class TestIndicatorCalculation:
    """测试技术指标计算（使用模拟数据）"""

    def test_indicators_with_mock_data(self):
        """测试使用模拟数据计算指标"""
        generator = MockKlineGenerator(base_price=50000.0)
        klines_raw = generator.generate_klines("BTCUSDT", "1h", count=100)

        # 转换为 KlineData 对象
        klines = []
        for data in klines_raw:
            kline = KlineData.from_binance_data("BTCUSDT", "1h", data)
            klines.append(kline)

        # 计算指标
        indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)

        assert indicators is not None
        assert "sma_7" in indicators
        assert "sma_20" in indicators
        assert "rsi_14" in indicators
        assert "current_price" in indicators

        # 检查指标值合理性
        assert indicators["sma_7"] > 0
        assert indicators["sma_20"] > 0
        assert 0 <= indicators["rsi_14"] <= 100
        assert indicators["current_price"] > 0

        print(f"\n技术指标：SMA7={indicators['sma_7']:.2f}, RSI={indicators['rsi_14']:.2f}")

    def test_trend_analysis(self):
        """测试趋势分析"""
        # 上涨趋势
        bull_generator = MockKlineGenerator(base_price=50000.0, trend=0.01)
        bull_klines = bull_generator.generate_klines("BTCUSDT", "1h", count=50)
        bull_prices = [float(k[4]) for k in bull_klines]

        # 下跌趋势
        bear_generator = MockKlineGenerator(base_price=50000.0, trend=-0.01)
        bear_klines = bear_generator.generate_klines("BTCUSDT", "1h", count=50)
        bear_prices = [float(k[4]) for k in bear_klines]

        # 转换为 KlineData
        bull_kline_objs = [
            KlineData.from_binance_data("BTCUSDT", "1h", k) for k in bull_klines
        ]
        bear_kline_objs = [
            KlineData.from_binance_data("BTCUSDT", "1h", k) for k in bear_klines
        ]

        # 计算指标
        bull_indicators = TechnicalIndicatorCalculator.calculate_all_indicators(
            bull_kline_objs
        )
        bear_indicators = TechnicalIndicatorCalculator.calculate_all_indicators(
            bear_kline_objs
        )

        # 上涨趋势的 RSI 应该较高
        assert bull_indicators["rsi_14"] > bear_indicators["rsi_14"]

        # 上涨趋势的当前价格应该高于起始价格
        assert bull_prices[-1] > bull_prices[0]
        assert bear_prices[-1] < bear_prices[0]

        print(
            f"\n上涨趋势 RSI={bull_indicators['rsi_14']:.2f}, "
            f"下跌趋势 RSI={bear_indicators['rsi_14']:.2f}"
        )


class TestIntegrationScenarios:
    """集成场景测试"""

    @pytest.mark.asyncio
    async def test_full_pipeline(self):
        """测试完整流程：生成->转换->计算指标"""
        # 1. 生成数据
        generator = MockKlineGenerator(base_price=50000.0)
        raw_klines = generator.generate_klines("BTCUSDT", "1h", count=100)

        # 2. 转换为模型
        klines = [
            KlineData.from_binance_data("BTCUSDT", "1h", k) for k in raw_klines
        ]

        # 3. 计算指标
        indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)

        # 4. 验证
        assert indicators is not None
        assert len(indicators) > 10

        print(f"\n完整流程测试通过，计算出 {len(indicators)} 个指标")

    def test_multi_symbol_pipeline(self):
        """测试多币种并行处理"""
        generators = create_mock_generators()
        all_indicators = {}

        for symbol, gen in generators.items():
            # 生成数据
            raw_klines = gen.generate_klines(symbol, "1h", count=50)

            # 转换并计算
            klines = [
                KlineData.from_binance_data(symbol, "1h", k) for k in raw_klines
            ]
            indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)

            all_indicators[symbol] = indicators

        # 验证所有币种都有指标
        assert len(all_indicators) == 3
        assert all(ind is not None for ind in all_indicators.values())

        print(f"\n多币种测试：{list(all_indicators.keys())}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
