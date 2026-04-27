#!/usr/bin/env python3
"""
评分引擎单元测试

测试评分引擎的核心功能
"""

import unittest
from unittest.mock import MagicMock, patch
from core.scoring import (
    ScoringEngineV612,
    get_scoring_engine,
    create_scoring_engine,
    list_available_versions,
)


class TestScoringEngineFactory(unittest.TestCase):
    """测试评分引擎工厂"""

    def test_create_default_engine(self):
        """测试创建默认引擎"""
        engine = create_scoring_engine()
        self.assertIsInstance(engine, ScoringEngineV612)

    def test_create_v612_engine(self):
        """测试创建v6.12引擎"""
        engine = create_scoring_engine('v6.12')
        self.assertIsInstance(engine, ScoringEngineV612)

    def test_create_latest_engine(self):
        """测试创建最新版本引擎"""
        engine = create_scoring_engine('latest')
        self.assertIsInstance(engine, ScoringEngineV612)

    def test_create_production_engine(self):
        """测试创建生产环境引擎"""
        engine = create_scoring_engine('production')
        self.assertIsInstance(engine, ScoringEngineV612)

    def test_get_scoring_engine(self):
        """测试获取评分引擎"""
        engine = get_scoring_engine()
        self.assertIsInstance(engine, ScoringEngineV612)

    def test_get_scoring_engine_v612(self):
        """测试获取v6.12引擎"""
        engine = get_scoring_engine()
        self.assertIsInstance(engine, ScoringEngineV612)

    def test_list_available_versions(self):
        """测试列出可用版本"""
        versions = list_available_versions()
        self.assertIn('v6.12', versions)
        self.assertIn('latest', versions)
        self.assertIn('production', versions)

    def test_invalid_version(self):
        """测试无效版本"""
        with self.assertRaises(ValueError):
            create_scoring_engine('invalid_version')


class TestScoringEngineV612(unittest.TestCase):
    """测试v6.12评分引擎"""

    def setUp(self):
        """测试前准备"""
        self.engine = ScoringEngineV612()

    def test_init(self):
        """测试初始化"""
        self.assertIsNotNone(self.engine.config)
        self.assertIn('scoring', self.engine.config)
        self.assertIn('weights', self.engine.config['scoring'])

    def test_score_with_veto(self):
        """测试一票否决"""
        data = {
            'funding_rate': 0.001,  # 超过阈值
            'indicators': {}
        }
        result = self.engine.score('BTCUSDT', data)
        self.assertEqual(result['score'], 0.0)
        self.assertIsNone(result['grade'])
        self.assertIn('veto_reason', result)

    def test_score_with_missing_data(self):
        """测试数据缺失"""
        data = {}
        result = self.engine.score('BTCUSDT', data)
        self.assertEqual(result['score'], 0.0)
        self.assertIsNone(result['grade'])
        self.assertIn('veto_reason', result)

    def test_score_with_valid_data(self):
        """测试有效数据评分"""
        data = {
            'funding_rate': 0.0001,
            'indicators': {
                '1d': {
                    'adx': [20],
                    'volume': [100] * 20 + [200, 250, 300, 280, 260],  # 量比 > 1.5
                    'atr14': [1000],
                    'close': [50000],
                    'ema21_list': [50000, 50100, 50200, 50300, 50400],
                    'rsi': 55,
                    'bollinger': {
                        'lower': [49000],
                        'upper': [51000]
                    },
                    'macd': {
                        'macd': 100,
                        'signal': 90
                    },
                    'close_list': [50000, 50100, 50200, 50300, 50400]
                },
                '4h': {
                    'ema21_list': [50000, 50100, 50200, 50300, 50400]
                },
                '1h': {
                    'ema21_list': [50000, 50100, 50200, 50300, 50400]
                }
            }
        }
        result = self.engine.score('BTCUSDT', data)
        self.assertGreater(result['score'], 0)
        self.assertIsNotNone(result['grade'])
        self.assertIn('direction', result)
        self.assertIn('position_ratio', result)
        self.assertIn('breakdown', result)

    def test_calculate_position_ratio(self):
        """测试仓位计算"""
        # S级
        ratio = self.engine._calculate_position_ratio(80, 'S')
        self.assertGreater(ratio, 0)
        self.assertLessEqual(ratio, 0.50)

        # A级
        ratio = self.engine._calculate_position_ratio(70, 'A')
        self.assertGreater(ratio, 0)
        self.assertLessEqual(ratio, 0.35)

        # B级
        ratio = self.engine._calculate_position_ratio(60, 'B')
        self.assertGreater(ratio, 0)
        self.assertLessEqual(ratio, 0.18)

        # C级
        ratio = self.engine._calculate_position_ratio(50, 'C')
        self.assertGreater(ratio, 0)
        self.assertLessEqual(ratio, 0.06)

        # None
        ratio = self.engine._calculate_position_ratio(40, None)
        self.assertEqual(ratio, 0.0)

    def test_determine_direction(self):
        """测试方向判断"""
        indicators = {
            '1d': {'ema21': [50000, 50100]},
            '4h': {'ema21': [50000, 50100]},
            '1h': {'ema21': [50000, 50100]}
        }
        direction = self.engine._determine_direction(indicators)
        self.assertIn(direction, ['多', '空'])


if __name__ == '__main__':
    unittest.main()
