"""
网格策略回测引擎模块

提供网格策略的逐笔K线回测能力，包含：
- GridBacktestEngine: 核心回测引擎
- MetricsCalculator: 网格专属指标计算器
- MarketSegmenter: 市场状态分段器
"""

from ai_tuner.backtest.grid_backtest import GridBacktestEngine
from ai_tuner.backtest.market_segment import MarketSegment, MarketSegmenter
from ai_tuner.backtest.metrics import MetricsCalculator
from ai_tuner.backtest.models import (
    BacktestDataError,
    BacktestState,
    BacktestTimeoutError,
    FillRecord,
    GridParams,
)

__all__ = [
    "GridBacktestEngine",
    "GridParams",
    "FillRecord",
    "BacktestState",
    "BacktestTimeoutError",
    "BacktestDataError",
    "MetricsCalculator",
    "MarketSegmenter",
    "MarketSegment",
]