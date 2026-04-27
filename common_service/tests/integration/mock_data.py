"""模拟数据生成器"""

import random
from datetime import datetime, timedelta
from typing import List, Dict
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


class MockKlineGenerator:
    """模拟 K 线数据生成器"""

    def __init__(
        self,
        base_price: float = 50000.0,
        volatility: float = 0.02,
        trend: float = 0.0,
    ):
        """
        初始化生成器

        Args:
            base_price: 基础价格
            volatility: 波动率（0-1）
            trend: 趋势（-1 到 1，负数下跌，正数上涨）
        """
        self.base_price = base_price
        self.volatility = volatility
        self.trend = trend
        self.current_price = base_price

    def generate_kline(
        self,
        symbol: str,
        interval: str,
        open_time: int,
    ) -> List:
        """
        生成单条 K 线数据

        Args:
            symbol: 交易对
            interval: 时间间隔
            open_time: 开盘时间（毫秒）

        Returns:
            币安格式的 K 线数据（12 个字段）
        """
        # 保存开盘价
        open_price = self.current_price
        
        # 随机生成价格变化
        change_percent = (random.random() - 0.5) * 2 * self.volatility + self.trend
        close_price = open_price * (1 + change_percent)

        # 生成最高价和最低价
        high_price = max(open_price, close_price) * (
            1 + random.random() * self.volatility * 0.5
        )
        low_price = min(open_price, close_price) * (
            1 - random.random() * self.volatility * 0.5
        )

        # 生成成交量
        volume = random.uniform(100, 1000)
        quote_volume = volume * open_price
        trade_count = random.randint(50, 500)
        taker_buy_volume = volume * random.uniform(0.4, 0.6)
        taker_buy_quote_volume = taker_buy_volume * open_price

        # 收盘时间
        close_time = open_time + self._interval_to_milliseconds(interval) - 1

        # 更新当前价格（为下一条 K 线做准备）
        self.current_price = close_price

        return [
            open_time,  # 开盘时间
            f"{open_price:.8f}",  # 开盘价
            f"{high_price:.8f}",  # 最高价
            f"{low_price:.8f}",  # 最低价
            f"{close_price:.8f}",  # 收盘价
            f"{volume:.8f}",  # 成交量
            close_time,  # 收盘时间
            f"{quote_volume:.8f}",  # 成交额
            trade_count,  # 成交笔数
            f"{taker_buy_volume:.8f}",  # 主动买入成交量
            f"{taker_buy_quote_volume:.8f}",  # 主动买入成交额
            "0",  # 忽略字段
        ]

    def generate_klines(
        self,
        symbol: str,
        interval: str,
        count: int,
        start_time: int = None,
    ) -> List[List]:
        """
        生成多条 K 线数据

        Args:
            symbol: 交易对
            interval: 时间间隔
            count: 数量
            start_time: 开始时间（毫秒）

        Returns:
            K 线数据列表
        """
        if start_time is None:
            # 默认从 1 小时前开始
            start_time = int(
                (datetime.now() - timedelta(hours=1)).timestamp() * 1000
            )

        interval_ms = self._interval_to_milliseconds(interval)
        klines = []

        for i in range(count):
            open_time = start_time + i * interval_ms
            kline = self.generate_kline(symbol, interval, open_time)
            klines.append(kline)

        return klines

    def _interval_to_milliseconds(self, interval: str) -> int:
        """将时间间隔转换为毫秒"""
        if interval.endswith("m"):
            minutes = int(interval[:-1])
            return minutes * 60 * 1000
        elif interval.endswith("h"):
            hours = int(interval[:-1])
            return hours * 60 * 60 * 1000
        elif interval.endswith("d"):
            days = int(interval[:-1])
            return days * 24 * 60 * 60 * 1000
        else:
            return 60 * 60 * 1000  # 默认 1 小时

    def reset(self, base_price: float = None):
        """重置生成器"""
        if base_price:
            self.base_price = base_price
        self.current_price = self.base_price


class MockNotificationSender:
    """模拟通知发送器（用于测试通知服务）"""

    def __init__(self):
        self.sent_messages = []
        self.send_count = 0

    async def send(self, project: str, message: str, **kwargs) -> bool:
        """模拟发送通知"""
        self.send_count += 1
        self.sent_messages.append(
            {
                "project": project,
                "message": message,
                "timestamp": datetime.now(),
                **kwargs,
            }
        )
        return True

    def get_sent_messages(self) -> List[Dict]:
        """获取已发送的消息"""
        return self.sent_messages

    def clear(self):
        """清空记录"""
        self.sent_messages = []
        self.send_count = 0


# 预定义的模拟数据生成器配置
MOCK_CONFIG = {
    "BTCUSDT": {"base_price": 50000.0, "volatility": 0.02, "trend": 0.001},
    "ETHUSDT": {"base_price": 3000.0, "volatility": 0.03, "trend": 0.0},
    "BNBUSDT": {"base_price": 300.0, "volatility": 0.025, "trend": -0.001},
}


def create_mock_generators() -> Dict[str, MockKlineGenerator]:
    """创建所有币种的模拟生成器"""
    generators = {}
    for symbol, config in MOCK_CONFIG.items():
        generators[symbol] = MockKlineGenerator(**config)
    return generators


if __name__ == "__main__":
    # 测试生成器
    generator = MockKlineGenerator(base_price=50000.0)
    klines = generator.generate_klines("BTCUSDT", "1h", count=10)

    print(f"生成了 {len(klines)} 条 K 线数据")
    print(f"第一条：开盘={klines[0][1]}, 收盘={klines[0][4]}")
    print(f"最后一条：开盘={klines[-1][1]}, 收盘={klines[-1][4]}")
