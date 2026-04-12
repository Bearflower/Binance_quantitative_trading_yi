"""
做空策略回测系统 - V2.0
"""

from .technical_indicators import (
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    calculate_atr,
    calculate_bollinger_bands,
    calculate_parabolic_sar,
    calculate_ema_slope,
    calculate_volume_ratio,
    is_bullish_engulfing,
    is_bearish_engulfing,
    calculate_ema_trend
)

from .scoring_simulator import ScoringSimulator
from .short_selling_backtester_v4 import ShortSellingBacktesterV4
from .performance_analyzer import PerformanceAnalyzer
from .report_generator import ReportGenerator

__all__ = [
    'ScoringSimulator',
    'ShortSellingBacktesterV4',
    'PerformanceAnalyzer',
    'ReportGenerator',
]
