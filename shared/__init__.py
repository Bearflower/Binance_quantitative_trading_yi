"""
共享核心模块
提供统一的API封装、数据服务、通知服务、配置加载等
"""

__version__ = "1.0.0"
__author__ = "Trading System Team"

from .binance_api import BinanceClient
from .config_loader import load_strategy_config, deep_merge
from .kline_service import KLineService
from .notification import NotificationClient
from .database import DatabaseManager
from .indicators import TechnicalIndicators
from .trade_logger import TradeLogger, TradeRecord

__all__ = [
    "BinanceClient",
    "KLineService",
    "NotificationClient",
    "DatabaseManager",
    "TechnicalIndicators",
    "TradeLogger",
    "TradeRecord",
    "load_strategy_config",
    "deep_merge",
]
