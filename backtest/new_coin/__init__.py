"""
新币做空策略回测框架
"""
from .backtest_engine import BacktestEngine
from .data_loader import DataLoader
from .order_executor import OrderExecutor
from .position_manager import PositionManager
from .statistics_analyzer import StatisticsAnalyzer
from .report_generator import ReportGenerator

__version__ = '1.0.0'
__all__ = [
    'BacktestEngine',
    'DataLoader',
    'OrderExecutor',
    'PositionManager',
    'StatisticsAnalyzer',
    'ReportGenerator'
]
