"""集成测试 - 通知服务（模拟）"""

import pytest
import asyncio
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from tests.integration.mock_data import MockNotificationSender


class TestMockNotificationSender:
    """测试模拟通知发送器"""

    @pytest.mark.asyncio
    async def test_send_message(self):
        """测试发送消息"""
        sender = MockNotificationSender()
        success = await sender.send("btc_eth", "测试消息")

        assert success is True
        assert sender.send_count == 1
        assert len(sender.sent_messages) == 1

        msg = sender.sent_messages[0]
        assert msg["project"] == "btc_eth"
        assert msg["message"] == "测试消息"
        assert "timestamp" in msg

        print(f"\n发送消息成功：{msg}")

    @pytest.mark.asyncio
    async def test_send_multiple_messages(self):
        """测试发送多条消息"""
        sender = MockNotificationSender()

        for i in range(10):
            await sender.send(f"project_{i % 3}", f"消息 {i}")

        assert sender.send_count == 10
        assert len(sender.sent_messages) == 10

        print(f"\n发送 {sender.send_count} 条消息成功")

    @pytest.mark.asyncio
    async def test_get_messages_by_project(self):
        """测试按项目获取消息"""
        sender = MockNotificationSender()

        # 发送不同项目的消息
        await sender.send("btc_eth", "BTC 消息 1")
        await sender.send("btc_eth", "BTC 消息 2")
        await sender.send("eth", "ETH 消息 1")
        await sender.send("grid", "网格消息 1")

        # 获取 btc_eth 项目的消息
        btc_messages = [
            m for m in sender.get_sent_messages() if m["project"] == "btc_eth"
        ]

        assert len(btc_messages) == 2
        assert all(m["project"] == "btc_eth" for m in btc_messages)

        print(f"\nbtc_eth 项目消息：{len(btc_messages)} 条")

    @pytest.mark.asyncio
    async def test_clear_messages(self):
        """测试清空消息"""
        sender = MockNotificationSender()

        for i in range(5):
            await sender.send("test", f"消息 {i}")

        assert sender.send_count == 5
        assert len(sender.sent_messages) == 5

        # 清空
        sender.clear()

        assert sender.send_count == 0
        assert len(sender.sent_messages) == 0

        print("\n清空消息成功")


class TestNotificationScenarios:
    """通知场景测试"""

    @pytest.mark.asyncio
    async def test_price_alert(self):
        """测试价格预警通知"""
        sender = MockNotificationSender()

        # 模拟价格突破预警
        symbol = "BTCUSDT"
        current_price = 51000
        target_price = 50000

        # 简化逻辑，直接发送预警
        message = f"⚠️ {symbol} 价格上涨突破预警：当前 ${current_price}, 目标 ${target_price}"
        await sender.send("btc_eth", message, level="warning")

        assert sender.send_count == 1
        assert "预警" in sender.sent_messages[0]["message"]

        print(f"\n价格预警：{sender.sent_messages[0]['message']}")

    @pytest.mark.asyncio
    async def test_trading_signal(self):
        """测试交易信号通知"""
        sender = MockNotificationSender()

        # 模拟交易信号
        signals = [
            {"symbol": "BTCUSDT", "action": "BUY", "price": 50000},
            {"symbol": "ETHUSDT", "action": "SELL", "price": 3000},
        ]

        for signal in signals:
            message = (
                f"📈 {signal['symbol']} {signal['action']} 信号 @ ${signal['price']}"
            )
            await sender.send("btc_eth", message, level="info")

        assert sender.send_count == 2
        assert all("信号" in m["message"] for m in sender.sent_messages)

        print(f"\n交易信号：{sender.send_count} 条")

    @pytest.mark.asyncio
    async def test_error_notification(self):
        """测试错误通知"""
        sender = MockNotificationSender()

        # 模拟系统错误
        error_msg = "数据库连接失败"
        message = f"❌ 系统错误：{error_msg}"
        await sender.send("admin", message, level="error")

        assert sender.send_count == 1
        assert "错误" in sender.sent_messages[0]["message"]

        print(f"\n错误通知：{sender.sent_messages[0]['message']}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
