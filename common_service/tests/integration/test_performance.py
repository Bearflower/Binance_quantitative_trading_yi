"""性能压力测试"""

import pytest
import asyncio
import time
import sys
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from tests.integration.mock_data import MockKlineGenerator, MockNotificationSender
from kline_data_service.models.kline import KlineData
from kline_data_service.core.indicator import TechnicalIndicatorCalculator


class TestDataGenerationPerformance:
    """测试数据生成性能"""

    def test_generate_1000_klines(self):
        """测试生成 1000 条 K 线的性能"""
        generator = MockKlineGenerator(base_price=50000.0)

        start_time = time.time()
        klines = generator.generate_klines("BTCUSDT", "1h", count=1000)
        end_time = time.time()

        elapsed = end_time - start_time

        assert len(klines) == 1000
        assert elapsed < 1.0, f"生成 1000 条 K 线耗时过长：{elapsed:.2f}秒"

        print(f"\n生成 1000 条 K 线：{elapsed:.3f}秒 ({1000/elapsed:.0f} 条/秒)")

    def test_generate_multi_symbol(self):
        """测试多币种并行生成性能"""
        symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
        generators = {s: MockKlineGenerator(base_price=50000.0) for s in symbols}

        start_time = time.time()

        # 串行生成
        all_klines = {}
        for symbol in symbols:
            klines = generators[symbol].generate_klines(symbol, "1h", count=100)
            all_klines[symbol] = klines

        end_time = time.time()
        elapsed = end_time - start_time

        assert all(len(k) == 100 for k in all_klines.values())
        assert elapsed < 1.0

        print(f"\n多币种生成 300 条 K 线：{elapsed:.3f}秒")


class TestIndicatorCalculationPerformance:
    """测试指标计算性能"""

    def test_calculate_indicators_1000_klines(self):
        """测试 1000 条 K 线的指标计算性能"""
        generator = MockKlineGenerator(base_price=50000.0)
        klines_raw = generator.generate_klines("BTCUSDT", "1h", count=1000)
        klines = [KlineData.from_binance_data("BTCUSDT", "1h", k) for k in klines_raw]

        start_time = time.time()
        indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)
        elapsed = time.time() - start_time

        assert indicators is not None
        assert len(indicators) > 10
        assert elapsed < 1.0, f"指标计算耗时过长：{elapsed:.2f}秒"

        print(f"\n1000 条 K 线指标计算：{elapsed:.3f}秒")

    def test_calculate_indicators_batch(self):
        """测试批量计算指标性能"""
        generators = {
            "BTCUSDT": MockKlineGenerator(base_price=50000.0),
            "ETHUSDT": MockKlineGenerator(base_price=3000.0),
            "BNBUSDT": MockKlineGenerator(base_price=300.0),
        }

        start_time = time.time()

        all_indicators = {}
        for symbol, gen in generators.items():
            klines_raw = gen.generate_klines(symbol, "1h", count=200)
            klines = [KlineData.from_binance_data(symbol, "1h", k) for k in klines_raw]
            indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)
            all_indicators[symbol] = indicators

        elapsed = time.time() - start_time

        assert len(all_indicators) == 3
        assert all(ind is not None for ind in all_indicators.values())
        assert elapsed < 1.0

        print(f"\n3 币种批量指标计算：{elapsed:.3f}秒")


class TestNotificationPerformance:
    """测试通知服务性能"""

    @pytest.mark.asyncio
    async def test_send_100_notifications(self):
        """测试发送 100 条通知的性能"""
        notifier = MockNotificationSender()

        start_time = time.time()

        for i in range(100):
            await notifier.send("test", f"测试消息 {i}")

        elapsed = time.time() - start_time

        assert notifier.send_count == 100
        assert elapsed < 1.0, f"发送 100 条通知耗时过长：{elapsed:.2f}秒"

        print(f"\n发送 100 条通知：{elapsed:.3f}秒 ({100/elapsed:.0f} 条/秒)")

    @pytest.mark.asyncio
    async def test_concurrent_notifications(self):
        """测试并发发送通知性能"""
        notifier = MockNotificationSender()

        async def send_batch(batch_id: int):
            for i in range(10):
                await notifier.send(f"project_{batch_id}", f"消息 {batch_id}-{i}")

        start_time = time.time()

        # 并发执行 10 个任务
        tasks = [send_batch(i) for i in range(10)]
        await asyncio.gather(*tasks)

        elapsed = time.time() - start_time

        assert notifier.send_count == 100
        assert elapsed < 1.0

        print(f"\n并发发送 100 条通知：{elapsed:.3f}秒")


class TestEndToEndPerformance:
    """测试端到端性能"""

    @pytest.mark.asyncio
    async def test_full_pipeline_performance(self):
        """测试完整流程性能（生成->转换->计算->通知）"""
        generator = MockKlineGenerator(base_price=50000.0)
        notifier = MockNotificationSender()

        start_time = time.time()

        # 1. 生成数据
        klines_raw = generator.generate_klines("BTCUSDT", "1h", count=200)

        # 2. 转换为模型
        klines = [KlineData.from_binance_data("BTCUSDT", "1h", k) for k in klines_raw]

        # 3. 计算指标
        indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)

        # 4. 发送通知
        message = (
            f"BTCUSDT: 价格=${indicators['current_price']:.2f}, "
            f"RSI={indicators['rsi_14']:.2f}"
        )
        await notifier.send("btc_eth", message)

        elapsed = time.time() - start_time

        assert indicators is not None
        assert notifier.send_count == 1
        assert elapsed < 1.0

        print(f"\n端到端流程：{elapsed:.3f}秒")

    def test_large_dataset_processing(self):
        """测试大数据集处理性能"""
        generator = MockKlineGenerator(base_price=50000.0)

        # 生成 10000 条 K 线
        start_time = time.time()
        klines_raw = generator.generate_klines("BTCUSDT", "1h", count=10000)
        klines = [KlineData.from_binance_data("BTCUSDT", "1h", k) for k in klines_raw]
        indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)
        elapsed = time.time() - start_time

        assert len(klines) == 10000
        assert indicators is not None
        assert elapsed < 5.0, f"处理 10000 条 K 线耗时过长：{elapsed:.2f}秒"

        print(f"\n处理 10000 条 K 线：{elapsed:.3f}秒")


class TestStressTest:
    """压力测试"""

    @pytest.mark.asyncio
    async def test_high_load_notifications(self):
        """高负载通知测试（1000 条）"""
        notifier = MockNotificationSender()

        start_time = time.time()

        for i in range(1000):
            await notifier.send("stress_test", f"压力测试消息 {i}")

        elapsed = time.time() - start_time

        assert notifier.send_count == 1000
        print(f"\n高负载测试：发送 1000 条通知耗时 {elapsed:.3f}秒 ({1000/elapsed:.0f} 条/秒)")

    def test_memory_efficiency(self):
        """测试内存效率"""
        import tracemalloc

        tracemalloc.start()

        generator = MockKlineGenerator(base_price=50000.0)
        klines_raw = generator.generate_klines("BTCUSDT", "1h", count=1000)
        klines = [KlineData.from_binance_data("BTCUSDT", "1h", k) for k in klines_raw]
        indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert indicators is not None
        # 峰值内存应该小于 50MB
        assert peak < 50 * 1024 * 1024, f"内存使用过高：{peak / 1024 / 1024:.2f}MB"

        print(f"\n内存效率：峰值 {peak / 1024 / 1024:.2f}MB")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
