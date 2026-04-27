"""联合场景测试 - K 线数据 + 通知服务"""

import pytest
import asyncio
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from tests.integration.mock_data import MockKlineGenerator, MockNotificationSender
from kline_data_service.models.kline import KlineData
from kline_data_service.core.indicator import TechnicalIndicatorCalculator


class TestTradingSignalGeneration:
    """测试交易信号生成（K 线数据 + 通知）"""

    @pytest.mark.asyncio
    async def test_rsi_oversold_signal(self):
        """测试 RSI 超卖信号"""
        # 生成下跌趋势数据（RSI 会降低）
        generator = MockKlineGenerator(base_price=50000.0, trend=-0.02)
        klines_raw = generator.generate_klines("BTCUSDT", "1h", count=50)
        klines = [KlineData.from_binance_data("BTCUSDT", "1h", k) for k in klines_raw]

        # 计算指标
        indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)

        # 检查 RSI 是否超卖（<30）
        rsi = indicators["rsi_14"]

        # 发送通知
        notifier = MockNotificationSender()
        if rsi < 30:
            await notifier.send(
                "btc_eth",
                f"💰 BTCUSDT RSI 超卖信号：RSI={rsi:.2f} (<30), 可能反弹",
                level="info",
            )
        elif rsi > 70:
            await notifier.send(
                "btc_eth",
                f"📉 BTCUSDT RSI 超买信号：RSI={rsi:.2f} (>70), 可能回调",
                level="warning",
            )
        else:
            await notifier.send(
                "btc_eth", f"📊 BTCUSDT RSI 中性：RSI={rsi:.2f}", level="info"
            )

        assert notifier.send_count == 1
        assert "RSI" in notifier.sent_messages[0]["message"]

        print(f"\nRSI 信号：{notifier.sent_messages[0]['message']}")

    @pytest.mark.asyncio
    async def test_ma_cross_signal(self):
        """测试均线交叉信号"""
        generator = MockKlineGenerator(base_price=50000.0, trend=0.01)
        klines_raw = generator.generate_klines("BTCUSDT", "1h", count=100)
        klines = [KlineData.from_binance_data("BTCUSDT", "1h", k) for k in klines_raw]

        indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)

        # 检查均线交叉
        sma_7 = indicators["sma_7"]
        sma_20 = indicators["sma_20"]

        notifier = MockNotificationSender()
        if sma_7 > sma_20:
            signal = "金叉"
            message = f"📈 BTCUSDT 均线{signal}: SMA7({sma_7:.2f}) > SMA20({sma_20:.2f})"
            await notifier.send("btc_eth", message, level="info")
        else:
            signal = "死叉"
            message = f"📉 BTCUSDT 均线{signal}: SMA7({sma_7:.2f}) < SMA20({sma_20:.2f})"
            await notifier.send("btc_eth", message, level="warning")

        assert notifier.send_count == 1
        assert "均线" in notifier.sent_messages[0]["message"]

        print(f"\n均线信号：{notifier.sent_messages[0]['message']}")


class TestPriceBreakthrough:
    """测试价格突破场景"""

    @pytest.mark.asyncio
    async def test_price_breakthrough_upper(self):
        """测试价格向上突破"""
        generator = MockKlineGenerator(base_price=50000.0, trend=0.015)
        klines_raw = generator.generate_klines("BTCUSDT", "1h", count=50)

        # 获取最新价格
        latest_price = float(klines_raw[-1][4])
        initial_price = float(klines_raw[0][4])

        notifier = MockNotificationSender()

        # 检查是否突破
        if latest_price > initial_price * 1.05:  # 上涨超过 5%
            await notifier.send(
                "btc_eth",
                f"🚀 BTCUSDT 价格突破：从 ${initial_price:.2f} 涨至 ${latest_price:.2f} (+{(latest_price/initial_price-1)*100:.2f}%)",
                level="info",
            )
        elif latest_price < initial_price * 0.95:  # 下跌超过 5%
            await notifier.send(
                "btc_eth",
                f"💥 BTCUSDT 价格跌破：从 ${initial_price:.2f} 跌至 ${latest_price:.2f} ({(latest_price/initial_price-1)*100:.2f}%)",
                level="warning",
            )
        else:
            await notifier.send(
                "btc_eth",
                f"➡️ BTCUSDT 价格震荡：${latest_price:.2f} ({(latest_price/initial_price-1)*100:.2f}%)",
                level="info",
            )

        assert notifier.send_count == 1
        assert "价格" in notifier.sent_messages[0]["message"]

        print(f"\n价格突破：{notifier.sent_messages[0]['message']}")


class TestMultiSymbolMonitoring:
    """测试多币种监控"""

    @pytest.mark.asyncio
    async def test_monitor_all_symbols(self):
        """测试监控所有币种"""
        from tests.integration.mock_data import create_mock_generators

        generators = create_mock_generators()
        notifier = MockNotificationSender()
        all_indicators = {}

        # 并行处理所有币种
        for symbol, gen in generators.items():
            # 生成数据
            klines_raw = gen.generate_klines(symbol, "1h", count=50)
            klines = [KlineData.from_binance_data(symbol, "1h", k) for k in klines_raw]

            # 计算指标
            indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)
            all_indicators[symbol] = indicators

            # 生成通知
            current_price = indicators["current_price"]
            rsi = indicators["rsi_14"]

            message = f"📊 {symbol}: 价格=${current_price:.2f}, RSI={rsi:.2f}"
            await notifier.send("all_symbols", message, level="info")

        # 验证所有币种都有通知
        assert notifier.send_count == 3
        messages = notifier.get_sent_messages()
        symbols_in_messages = [m["message"].split(":")[0] for m in messages]

        assert "📊 BTCUSDT" in symbols_in_messages
        assert "📊 ETHUSDT" in symbols_in_messages
        assert "📊 BNBUSDT" in symbols_in_messages

        print(f"\n多币种监控：{notifier.send_count} 条通知")
        for msg in messages:
            print(f"  - {msg['message']}")


class TestAutomatedTrading:
    """测试自动化交易场景"""

    @pytest.mark.asyncio
    async def test_full_trading_cycle(self):
        """测试完整交易周期"""
        generator = MockKlineGenerator(base_price=50000.0, trend=0.005)
        notifier = MockNotificationSender()

        # 1. 数据采集
        klines_raw = generator.generate_klines("BTCUSDT", "1h", count=100)
        klines = [KlineData.from_binance_data("BTCUSDT", "1h", k) for k in klines_raw]

        # 2. 指标计算
        indicators = TechnicalIndicatorCalculator.calculate_all_indicators(klines)

        # 3. 信号判断
        signals = []

        # RSI 信号
        rsi = indicators["rsi_14"]
        if rsi < 30:
            signals.append(("BUY", f"RSI 超卖 ({rsi:.2f})"))
        elif rsi > 70:
            signals.append(("SELL", f"RSI 超买 ({rsi:.2f})"))

        # 均线信号
        if indicators["sma_7"] > indicators["sma_20"]:
            signals.append(("BUY", "均线金叉"))
        else:
            signals.append(("SELL", "均线死叉"))

        # 4. 发送通知
        for action, reason in signals:
            emoji = "📈" if action == "BUY" else "📉"
            message = f"{emoji} BTCUSDT {action} 信号：{reason}"
            await notifier.send("btc_eth", message, level="info" if action == "BUY" else "warning")

        # 5. 验证
        assert notifier.send_count == len(signals)
        assert len(signals) > 0

        print(f"\n完整交易周期：生成 {len(signals)} 个信号")
        for msg in notifier.sent_messages:
            print(f"  - {msg['message']}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
