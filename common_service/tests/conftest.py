"""
测试配置
"""

import pytest
import asyncio
from typing import AsyncGenerator, Generator


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """创建事件循环"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def sample_kline_data():
    """示例 K 线数据"""
    return {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "open_time": "2026-04-20T10:00:00Z",
        "close_time": "2026-04-20T11:00:00Z",
        "open": 95000.0,
        "high": 95500.0,
        "low": 94800.0,
        "close": 95200.0,
        "volume": 1234.56,
        "quote_volume": 117654321.0,
        "trades_count": 5678
    }


@pytest.fixture
def sample_notification():
    """示例通知消息"""
    return {
        "project": "btc_eth",
        "message": "测试消息",
        "type": "text",
        "level": "info"
    }
