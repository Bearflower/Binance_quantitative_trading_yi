"""
形态检测单元测试
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from strategy.pattern_detector import PatternDetector
from strategy.scoring import PatternScorer


def generate_sample_kline(days=120, base_price=100):
    """生成模拟 K 线数据"""
    dates = pd.date_range(end=datetime.now(), periods=days, freq='B')
    
    np.random.seed(42)
    returns = np.random.randn(days) * 0.02
    
    close = base_price * np.cumprod(1 + returns)
    open_price = close * (1 + np.random.randn(days) * 0.01)
    high = np.maximum(open_price, close) * (1 + np.abs(np.random.randn(days) * 0.01))
    low = np.minimum(open_price, close) * (1 - np.abs(np.random.randn(days) * 0.01))
    volume = np.random.randint(100000, 1000000, days)
    
    df = pd.DataFrame({
        'date': dates,
        'open': open_price,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    })
    
    return df


def test_pattern_detector_initialization():
    """测试形态检测器初始化"""
    detector = PatternDetector()
    assert detector.params is not None
    assert 'drop_threshold' in detector.params
    assert detector.params['drop_threshold'] == 0.20


def test_pattern_detector_insufficient_data():
    """测试数据不足的情况"""
    detector = PatternDetector()
    df = generate_sample_kline(days=30)
    
    is_match, detail = detector.check_pattern(df)
    
    assert is_match == False
    assert 'reason' in detail
    assert detail['reason'] == '数据不足 60 天'


def test_scorer_initialization():
    """测试评分器初始化"""
    scorer = PatternScorer()
    assert scorer.weights is not None
    assert 'drop_depth' in scorer.weights
    assert scorer.weights['drop_depth'] == 0.25


def test_scorer_drop_depth():
    """测试跌幅深度评分"""
    scorer = PatternScorer()
    
    detail = {'drop_rate': 0.20}
    score = scorer._score_drop_depth(detail, {})
    assert score == 60
    
    detail = {'drop_rate': 0.30}
    score = scorer._score_drop_depth(detail, {})
    assert score == 100
    
    detail = {'drop_rate': 0.25}
    score = scorer._score_drop_depth(detail, {})
    assert 60 < score < 100


def test_scorer_shrink_degree():
    """测试缩量程度评分"""
    scorer = PatternScorer()
    
    detail = {'min_vol_ratio': 0.50}
    score = scorer._score_shrink_degree(detail, {})
    assert score == 60
    
    detail = {'min_vol_ratio': 0.30}
    score = scorer._score_shrink_degree(detail, {})
    assert score == 100


def test_scorer_surge_strength():
    """测试放量强度评分"""
    scorer = PatternScorer()
    
    detail = {'surge_volume_ratio': 1.5}
    score = scorer._score_surge_strength(detail, {})
    assert score == 60
    
    detail = {'surge_volume_ratio': 3.0}
    score = scorer._score_surge_strength(detail, {})
    assert score == 100


def test_full_scoring():
    """测试完整评分流程"""
    scorer = PatternScorer()
    
    detail = {
        'drop_rate': 0.25,
        'min_vol_ratio': 0.40,
        'surge_volume_ratio': 2.0,
        'surge_price': 105,
        'surge_open': 100,
        'low_after_surge': 102,
        'retrace_vol_ratio': 0.50
    }
    
    score = scorer.score(detail, {})
    
    assert 0 <= score <= 100
    assert isinstance(score, float)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
