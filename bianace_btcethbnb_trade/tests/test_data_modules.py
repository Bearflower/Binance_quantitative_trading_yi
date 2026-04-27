#!/usr/bin/env python3
"""
数据模块单元测试

测试数据获取、指标计算和缓存管理功能
"""

import unittest
from unittest.mock import MagicMock, patch
from datetime import timedelta
from core.data import MarketDataFetcher, DataCache, IndicatorCalculator, get_data_fetcher
import pandas as pd


class TestDataCache(unittest.TestCase):
    """测试数据缓存"""

    def setUp(self):
        """测试前准备"""
        self.cache = DataCache(maxsize=100, ttl_seconds=3600)  # 3600秒 = 1小时

    def test_init(self):
        """测试初始化"""
        self.assertIsNotNone(self.cache._cache)
        self.assertEqual(self.cache.maxsize, 100)
        self.assertEqual(self.cache.ttl_seconds, 3600)

    def test_set_and_get(self):
        """测试设置和获取"""
        data = {'symbol': 'BTCUSDT', 'price': 50000}
        self.cache.set('BTCUSDT', data)
        result = self.cache.get('BTCUSDT')
        self.assertEqual(result, data)

    def test_is_valid_empty(self):
        """测试空缓存有效性"""
        self.assertFalse(self.cache.is_valid())

    def test_is_valid_with_data(self):
        """测试有数据缓存有效性"""
        data = {'symbol': 'BTCUSDT', 'price': 50000}
        self.cache.set('BTCUSDT', data)
        self.assertTrue(self.cache.is_valid(['BTCUSDT']))

    def test_clear(self):
        """测试清除缓存"""
        data = {'symbol': 'BTCUSDT', 'price': 50000}
        self.cache.set('BTCUSDT', data)
        self.cache.clear()
        self.assertTrue(self.cache.is_empty())

    def test_has_symbol(self):
        """测试检查交易对是否存在"""
        data = {'symbol': 'BTCUSDT', 'price': 50000}
        self.cache.set('BTCUSDT', data)
        self.assertTrue(self.cache.has_symbol('BTCUSDT'))
        self.assertFalse(self.cache.has_symbol('ETHUSDT'))


class TestIndicatorCalculator(unittest.TestCase):
    """测试指标计算器"""

    def test_calculate_ema(self):
        """测试EMA计算"""
        data = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        ema = IndicatorCalculator.calculate_ema(data, period=5)
        self.assertIsInstance(ema, pd.Series)
        self.assertEqual(len(ema), len(data))

    def test_calculate_atr(self):
        """测试ATR计算"""
        high = pd.Series([11, 12, 13, 14, 15])
        low = pd.Series([9, 10, 11, 12, 13])
        close = pd.Series([10, 11, 12, 13, 14])
        atr = IndicatorCalculator.calculate_atr(high, low, close, period=3)
        self.assertIsInstance(atr, pd.Series)
        self.assertEqual(len(atr), len(close))

    def test_calculate_rsi(self):
        """测试RSI计算"""
        data = pd.Series([10, 11, 12, 11, 10, 11, 12, 13, 14, 13, 12, 13, 14, 15, 16])
        rsi = IndicatorCalculator.calculate_rsi(data, period=14)
        self.assertIsInstance(rsi, pd.Series)
        self.assertEqual(len(rsi), len(data))

    def test_calculate_timeframe_indicators(self):
        """测试时间框架指标计算"""
        kline_data = {
            'close': [100 + i for i in range(30)],
            'high': [105 + i for i in range(30)],
            'low': [95 + i for i in range(30)],
            'volume': [1000 for i in range(30)]
        }
        result = IndicatorCalculator.calculate_timeframe_indicators(kline_data, '1d')
        self.assertIsNotNone(result)
        self.assertIn('close', result)
        self.assertIn('ema21', result)
        self.assertIn('rsi', result)
        self.assertIn('atr14', result)


class TestMarketDataFetcher(unittest.TestCase):
    """测试数据获取器"""

    def test_init(self):
        """测试初始化"""
        fetcher = MarketDataFetcher()
        self.assertIsNotNone(fetcher.cache)

    def test_get_data_fetcher_singleton(self):
        """测试单例模式"""
        fetcher1 = get_data_fetcher()
        fetcher2 = get_data_fetcher()
        self.assertIs(fetcher1, fetcher2)

    @patch('core.data.fetcher.MarketDataFetcher._get_klines_from_service')
    def test_fetch_market_data(self, mock_get_klines):
        """测试获取市场数据"""
        # 模拟K线数据
        mock_get_klines.return_value = [
            {
                'open_price': 50000,
                'close_price': 50100,
                'high_price': 50200,
                'low_price': 49900,
                'volume': 1000
            }
            for _ in range(30)
        ]

        fetcher = MarketDataFetcher()
        data = fetcher.fetch_market_data(['BTCUSDT'])

        self.assertIsNotNone(data)
        self.assertIn('BTCUSDT', data)


if __name__ == '__main__':
    unittest.main()
