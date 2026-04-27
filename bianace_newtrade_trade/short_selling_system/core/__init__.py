"""
核心模块

导出所有核心组件
"""

from .binance_client import BinanceDataClient, binance_client
from .coingecko_client import CoinGeckoClient, coingecko_client
from .calculator import (
    calculate_oi_ratio,
    score_oi_ratio,
    calculate_annualized_funding_rate,
    score_funding_rate
)
from .unlock_manager import UnlockDataManager, unlock_manager
from .pattern_recognition import (
    PatternRecognition,
    pattern_recognition
)
from .listing_detector import NewListingDetector, listing_detector
from .cache import DataCache, data_cache, cached
from .scheduler import TaskScheduler, MonitoringScheduler, monitoring_scheduler
from .scoring_engine import ScoringEngine, ScoringResult, scoring_engine
from .signal_manager import SignalManager, Signal, SignalStatus, signal_manager
from .notifier import FeishuNotifier, feishu_notifier
from .cli import CommandHandler, cmd_handler
from .trading_executor import TradingExecutor, trading_executor

__all__ = [
    # 客户端
    'BinanceDataClient', 'binance_client',
    'CoinGeckoClient', 'coingecko_client',
    
    # 计算工具
    'calculate_oi_ratio', 'score_oi_ratio',
    'calculate_annualized_funding_rate', 'score_funding_rate',
    
    # 解锁管理
    'UnlockDataManager', 'unlock_manager',
    
    # 形态识别
    'PatternRecognition', 'pattern_recognition',
    
    # 新币检测
    'NewListingDetector', 'listing_detector',
    
    # 缓存
    'DataCache', 'data_cache', 'cached',
    
    # 调度器
    'TaskScheduler', 'MonitoringScheduler', 'monitoring_scheduler',
    
    # 评分引擎
    'ScoringEngine', 'ScoringResult', 'scoring_engine',
    
    # 信号管理
    'SignalManager', 'Signal', 'SignalStatus', 'signal_manager',
    
    # 通知推送
    'FeishuNotifier', 'feishu_notifier',
    
    # 命令行
    'CommandHandler', 'cmd_handler',
    
    # 交易执行
    'TradingExecutor', 'trading_executor',
]
