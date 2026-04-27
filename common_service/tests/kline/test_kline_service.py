"""K 线数据服务测试"""

import pytest
import sys
from pathlib import Path

# 添加 src 目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestBinanceClient:
    """测试币安 API 客户端"""

    @pytest.mark.asyncio
    async def test_get_klines(self):
        """测试获取 K 线数据"""
        from kline_data_service.core.binance_client import BinanceClient

        client = BinanceClient()
        await client.connect()

        try:
            # 获取 BTCUSDT 1 小时 K 线
            klines = await client.get_klines(
                symbol="BTCUSDT", interval="1h", limit=10
            )

            assert klines is not None
            assert len(klines) > 0
            assert len(klines[0]) == 12  # K 线数据有 12 个字段

            print(f"\n获取到 {len(klines)} 条 K 线数据")
            print(f"第一条 K 线：开盘价={klines[0][1]}, 收盘价={klines[0][4]}")

        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_get_symbol_info(self):
        """测试获取交易对信息"""
        from kline_data_service.core.binance_client import BinanceClient

        client = BinanceClient()
        await client.connect()

        try:
            info = await client.get_symbol_info("BTCUSDT")

            assert info is not None
            assert info["symbol"] == "BTCUSDT"
            assert "base_asset" in info
            assert "quote_asset" in info

            print(f"\nBTCUSDT 信息：{info}")

        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_get_server_time(self):
        """测试获取服务器时间"""
        from kline_data_service.core.binance_client import BinanceClient

        client = BinanceClient()
        await client.connect()

        try:
            server_time = await client.get_server_time()

            assert server_time is not None
            assert isinstance(server_time, int)
            assert server_time > 0

            print(f"\n币安服务器时间：{server_time}")

        finally:
            await client.disconnect()


class TestKlineDataModel:
    """测试 K 线数据模型"""

    def test_from_binance_data(self):
        """测试从币安数据创建模型"""
        from kline_data_service.models.kline import KlineData

        # 模拟币安 K 线数据
        binance_data = [
            1499040000000,  # 开盘时间
            "0.01634790",  # 开盘价
            "0.80000000",  # 最高价
            "0.01575800",  # 最低价
            "0.01577100",  # 收盘价
            "148976.11427815",  # 成交量
            1499644799999,  # 收盘时间
            "2434.19055334",  # 成交额
            300,  # 成交笔数
            "1756.87402397",  # 主动买入成交量
            "28.46694236",  # 主动买入成交额
            "17928899.62484339",
        ]

        kline = KlineData.from_binance_data("BTCUSDT", "1h", binance_data)

        assert kline.symbol == "BTCUSDT"
        assert kline.interval == "1h"
        assert kline.open_time == 1499040000000
        assert abs(kline.open_price - 0.01634790) < 0.00000001
        assert abs(kline.close_price - 0.01577100) < 0.00000001
        assert kline.trade_count == 300

        print(f"\nK 线模型：{kline}")

    def test_to_dict(self):
        """测试转换为字典"""
        from kline_data_service.models.kline import KlineData

        binance_data = [
            1499040000000,
            "0.01634790",
            "0.80000000",
            "0.01575800",
            "0.01577100",
            "148976.11427815",
            1499644799999,
            "2434.19055334",
            300,
            "1756.87402397",
            "28.46694236",
            "17928899.62484339",
        ]

        kline = KlineData.from_binance_data("BTCUSDT", "1h", binance_data)
        data_dict = kline.to_dict()

        assert "symbol" in data_dict
        assert "open_time" in data_dict
        assert "close_price" in data_dict

        print(f"\nK 线字典：{data_dict}")


class TestTechnicalIndicators:
    """测试技术指标计算"""

    def test_sma(self):
        """测试简单移动平均"""
        from kline_data_service.core.indicator import (
            TechnicalIndicatorCalculator,
        )

        prices = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

        sma_3 = TechnicalIndicatorCalculator.calculate_sma(prices, 3)
        sma_5 = TechnicalIndicatorCalculator.calculate_sma(prices, 5)

        assert sma_3 is not None
        assert abs(sma_3 - 9.0) < 0.0001  # (8+9+10)/3
        assert sma_5 is not None
        assert abs(sma_5 - 8.0) < 0.0001  # (6+7+8+9+10)/5

        print(f"\nSMA(3)={sma_3}, SMA(5)={sma_5}")

    def test_ema(self):
        """测试指数移动平均"""
        from kline_data_service.core.indicator import (
            TechnicalIndicatorCalculator,
        )

        prices = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

        ema_3 = TechnicalIndicatorCalculator.calculate_ema(prices, 3)

        assert ema_3 is not None
        print(f"\nEMA(3)={ema_3}")

    def test_rsi(self):
        """测试相对强弱指数"""
        from kline_data_service.core.indicator import (
            TechnicalIndicatorCalculator,
        )

        # 上涨趋势 - 需要至少 period + 1 = 15 个数据点
        prices = list(range(10, 30))  # 20 个数据点

        rsi = TechnicalIndicatorCalculator.calculate_rsi(prices, 14)

        assert rsi is not None
        assert 0 <= rsi <= 100
        print(f"\nRSI(14)={rsi}")

    def test_bollinger_bands(self):
        """测试布林带"""
        from kline_data_service.core.indicator import (
            TechnicalIndicatorCalculator,
        )

        prices = list(range(20, 40))  # 20 个数据

        bb = TechnicalIndicatorCalculator.calculate_bollinger_bands(
            prices, 20, 2.0
        )

        assert bb is not None
        assert "upper" in bb
        assert "middle" in bb
        assert "lower" in bb
        assert bb["upper"] > bb["middle"] > bb["lower"]

        print(f"\n布林带：上轨={bb['upper']}, 中轨={bb['middle']}, 下轨={bb['lower']}")

    def test_calculate_all_indicators(self):
        """测试计算所有指标"""
        from kline_data_service.core.indicator import (
            TechnicalIndicatorCalculator,
        )
        from kline_data_service.models.kline import KlineData

        # 创建模拟 K 线数据
        klines = []
        base_time = 1499040000000
        for i in range(50):
            kline = KlineData(
                symbol="BTCUSDT",
                interval="1h",
                open_time=base_time + i * 3600000,
                open_price=100 + i,
                high_price=105 + i,
                low_price=95 + i,
                close_price=102 + i,
                volume=1000 + i * 10,
                close_time=base_time + (i + 1) * 3600000,
                quote_volume=102000 + i * 1000,
                trade_count=100 + i,
                taker_buy_volume=500 + i * 5,
                taker_buy_quote_volume=51000 + i * 500,
            )
            klines.append(kline)

        indicators = (
            TechnicalIndicatorCalculator.calculate_all_indicators(klines)
        )

        assert indicators is not None
        assert "sma_7" in indicators
        assert "sma_20" in indicators
        assert "rsi_14" in indicators
        assert "macd" in indicators
        assert "bb_upper" in indicators
        assert "current_price" in indicators

        print(f"\n所有指标：{indicators}")


class TestKlineCollector:
    """测试 K 线采集器"""

    @pytest.mark.asyncio
    async def test_collector_initialization(self):
        """测试采集器初始化"""
        from kline_data_service.core.binance_client import BinanceClient
        from kline_data_service.core.collector import KlineCollector
        from shared.core.database import db_manager

        # 连接数据库
        await db_manager.connect()

        try:
            client = BinanceClient()
            await client.connect()

            collector = KlineCollector(
                binance_client=client,
                db=db_manager,
                symbols=["BTCUSDT"],
                intervals=["1h"],
            )

            assert collector.symbols == ["BTCUSDT"]
            assert collector.intervals == ["1h"]
            assert collector.running is False

            stats = collector.get_stats()
            assert "total_collected" in stats
            assert "total_stored" in stats
            assert "total_errors" in stats

            print(f"\n采集器统计：{stats}")

            await client.disconnect()
        finally:
            await db_manager.disconnect()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
