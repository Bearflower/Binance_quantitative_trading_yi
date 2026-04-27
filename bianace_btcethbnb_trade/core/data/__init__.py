#!/usr/bin/env python3
"""
数据模块

提供统一的数据获取、指标计算和缓存管理接口
"""

from core.data.fetcher import MarketDataFetcher, get_data_fetcher
from core.data.indicators import IndicatorCalculator
from core.data.cache import DataCache

__all__ = [
    'MarketDataFetcher',
    'get_data_fetcher',
    'IndicatorCalculator',
    'DataCache',
]
