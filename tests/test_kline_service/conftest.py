"""
K线服务测试通用配置
"""

import sys
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from datetime import datetime

# 添加项目根目录到 sys.path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 添加 services/kline_service 到 sys.path，使模块内导入能正确解析
KLINE_SERVICE_PATH = str(PROJECT_ROOT / "services" / "kline_service")
if KLINE_SERVICE_PATH not in sys.path:
    sys.path.insert(0, KLINE_SERVICE_PATH)

# 添加 shared 目录到 sys.path，使 shared.core.database 等导入能正确解析
SHARED_PATH = str(PROJECT_ROOT / "shared")
if SHARED_PATH not in sys.path:
    sys.path.insert(0, SHARED_PATH)

# ============================================================
# 模拟共享模块，避免真实的数据库连接和日志初始化
# ============================================================

# 模拟 shared.utils.logger
mock_logger_module = MagicMock()
mock_logger = MagicMock()
mock_logger_module.get_logger.return_value = mock_logger
sys.modules["shared.utils.logger"] = mock_logger_module

# 模拟 shared.utils（保留 retry_on_failure 作为透传装饰器，使被装饰的 async 函数保持协程特性）
mock_utils = MagicMock()
mock_utils.get_logger = mock_logger_module.get_logger
mock_utils.retry_on_failure.side_effect = lambda **kwargs: lambda f: f
sys.modules["shared.utils"] = mock_utils

# 模拟 shared.core.database
mock_db_module = MagicMock()
mock_db_manager = MagicMock()
mock_db_manager.get_connection = AsyncMock()
mock_db_manager.execute = AsyncMock()
mock_db_manager.fetch_all = AsyncMock()
mock_db_manager.fetch_one = AsyncMock()
mock_db_manager.fetch_val = AsyncMock()
mock_db_module.db_manager = mock_db_manager
mock_db_module.DatabaseManager = MagicMock
sys.modules["shared.core.database"] = mock_db_module

# 模拟 shared.core.config
mock_settings = MagicMock()
mock_settings.DATABASE_URL = "sqlite:///:memory:"
mock_settings.KLINE_TABLE_PREFIX = "klines_"
mock_settings.KLINE_SERVICE_PORT = 8000
mock_config_module = MagicMock()
mock_config_module.settings = mock_settings
sys.modules["shared.core.config"] = mock_config_module

# 模拟 shared.core
mock_core = MagicMock()
mock_core.database = mock_db_module
mock_core.config = mock_config_module
sys.modules["shared.core"] = mock_core

# ============================================================
# K 线数据模型
# ============================================================
from services.kline_service.models.kline import KlineData
from services.kline_service.models.registered_symbol import (
    RegisteredSymbolConfig, RegisterRequest, RenewRequest, UnregisterRequest
)


# ============================================================
# 测试数据
# ============================================================

@pytest.fixture
def mock_kline_data():
    """模拟币安 K 线原始数据（列表格式）"""
    return [
        [
            1700000000000,       # 开盘时间
            "50000.0",           # 开盘价
            "51000.0",           # 最高价
            "49000.0",           # 最低价
            "50500.0",           # 收盘价
            "100.5",             # 成交量
            1700003600000,       # 收盘时间
            "5000000.0",         # 成交额
            1000,                # 成交笔数
            "60.0",              # 主动买入成交量
            "3000000.0",         # 主动买入成交额
            "0"                  # 忽略字段
        ]
    ]


@pytest.fixture
def mock_kline_data_list():
    """模拟多条 K 线数据"""
    return [
        [
            1700000000000,
            "50000.0", "51000.0", "49000.0", "50500.0",
            "100.5", 1700003600000, "5000000.0", 1000,
            "60.0", "3000000.0", "0"
        ],
        [
            1700003600000,
            "50500.0", "51500.0", "49500.0", "51000.0",
            "200.3", 1700007200000, "10200000.0", 2000,
            "120.0", "6000000.0", "0"
        ],
    ]


@pytest.fixture
def mock_kline_objects():
    """模拟 KlineData 对象列表"""
    return [
        KlineData(
            symbol="BTCUSDT",
            interval="15m",
            open_time=1700000000000,
            open_price=50000.0,
            high_price=51000.0,
            low_price=49000.0,
            close_price=50500.0,
            volume=100.5,
            close_time=1700003600000,
            quote_volume=5000000.0,
            trade_count=1000,
            taker_buy_volume=60.0,
            taker_buy_quote_volume=3000000.0,
        ),
        KlineData(
            symbol="BTCUSDT",
            interval="15m",
            open_time=1700003600000,
            open_price=50500.0,
            high_price=51500.0,
            low_price=49500.0,
            close_price=51000.0,
            volume=200.3,
            close_time=1700007200000,
            quote_volume=10200000.0,
            trade_count=2000,
            taker_buy_volume=120.0,
            taker_buy_quote_volume=6000000.0,
        ),
    ]


@pytest.fixture
def mock_registered_symbol_config():
    """模拟已注册标的配置"""
    now = datetime.now()
    return RegisteredSymbolConfig(
        id=1,
        symbol="BTCUSDT",
        intervals=["15m", "1h", "4h"],
        registered_at=now,
        expires_at=now.replace(year=now.year + 1),
        duration_days=10,
        priority="high",
        status="active",
        created_by="system",
        updated_at=now,
    )


@pytest.fixture
def mock_register_request():
    """模拟注册请求"""
    return RegisterRequest(
        symbol="BTCUSDT",
        intervals=["15m", "1h", "4h"],
        duration_days=10,
        priority="high",
    )


@pytest.fixture
def mock_expired_config():
    """模拟已过期的标的配置"""
    now = datetime.now()
    return RegisteredSymbolConfig(
        id=2,
        symbol="ETHUSDT",
        intervals=["15m", "1h"],
        registered_at=now,
        expires_at=now.replace(year=now.year - 1),
        duration_days=1,
        priority="normal",
        status="active",
        created_by="system",
        updated_at=now,
    )