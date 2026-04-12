"""
核心模块单元测试合集
"""
import pytest
from datetime import datetime, timedelta
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.calculator import calculate_oi_mc_ratio, calculate_oi_mc_score
from core.pattern_recognition import (
    is_shooting_star, is_hammer, is_bullish_engulfing, 
    is_bearish_engulfing, analyze_kline_pattern
)
from core.scoring_engine import calculate_comprehensive_score


class TestCalculator:
    """OI/市值比率计算器测试"""
    
    def test_oi_mc_ratio_calculation(self):
        """测试 OI/市值比率计算"""
        oi_usd = 50_000_000  # 5000 万美金
        mc_usd = 100_000_000  # 1 亿美金
        
        ratio = calculate_oi_mc_ratio(oi_usd, mc_usd)
        
        assert ratio == 0.5
    
    def test_oi_mc_ratio_high_risk(self):
        """测试高风险比率"""
        oi_usd = 120_000_000  # 1.2 亿
        mc_usd = 100_000_000  # 1 亿
        
        ratio = calculate_oi_mc_ratio(oi_usd, mc_usd)
        
        assert ratio == 1.2
        assert ratio > 1.0  # 触发否决机制
    
    def test_oi_mc_score_low_ratio(self):
        """测试低比率得分（高风险高得分）"""
        ratio = 0.3  # 30% 比率
        
        score = calculate_oi_mc_score(ratio)
        
        assert score >= 8.0  # 应该高分
    
    def test_oi_mc_score_medium_ratio(self):
        """测试中等比率得分"""
        ratio = 0.6  # 60% 比率
        
        score = calculate_oi_mc_score(ratio)
        
        assert 5.0 <= score < 8.0
    
    def test_oi_mc_score_high_ratio(self):
        """测试高比率得分（低风险低得分）"""
        ratio = 1.2  # 120% 比率 - 触发否决
        
        score = calculate_oi_mc_score(ratio)
        
        assert score <= 3.0  # 应该低分


class TestPatternRecognition:
    """K 线形态识别测试"""
    
    def test_shooting_star_detection(self):
        """测试流星线（看跌）检测"""
        # 典型的流星线：长上影线，小实体，在下影线
        kline = {
            'open': 100.0,
            'high': 110.0,
            'low': 99.0,
            'close': 101.0
        }
        
        result = is_shooting_star(kline)
        
        assert result is True
    
    def test_shooting_star_not_pattern(self):
        """测试非流星线"""
        kline = {
            'open': 100.0,
            'high': 102.0,
            'low': 98.0,
            'close': 101.0
        }
        
        result = is_shooting_star(kline)
        
        assert result is False
    
    def test_hammer_detection(self):
        """测试锤头线（看涨）检测"""
        kline = {
            'open': 100.0,
            'high': 101.0,
            'low': 90.0,
            'close': 99.5
        }
        
        result = is_hammer(kline)
        
        assert result is True
    
    def test_bullish_engulfing(self):
        """测试看涨吞没形态"""
        prev_kline = {'open': 100.0, 'close': 95.0}  # 阴线
        curr_kline = {'open': 94.0, 'close': 101.0}  # 阳线，完全吞没
        
        result = is_bullish_engulfing(prev_kline, curr_kline)
        
        assert result is True
    
    def test_bearish_engulfing(self):
        """测试看跌吞没形态"""
        prev_kline = {'open': 95.0, 'close': 100.0}  # 阳线
        curr_kline = {'open': 101.0, 'close': 94.0}  # 阴线，完全吞没
        
        result = is_bearish_engulfing(prev_kline, curr_kline)
        
        assert result is True
    
    def test_analyze_kline_pattern_shooting_star(self):
        """测试 K 线形态分析 - 流星线"""
        klines = [
            {'open': 100.0, 'high': 102.0, 'low': 98.0, 'close': 101.0},
            {'open': 100.0, 'high': 110.0, 'low': 99.0, 'close': 101.0},  # 流星线
        ]
        
        pattern = analyze_kline_pattern(klines)
        
        assert 'shooting_star' in pattern


class TestScoringEngine:
    """综合评分引擎测试"""
    
    def test_comprehensive_score_calculation(self):
        """测试综合评分计算"""
        contract_data = {
            'oi_mc_ratio': 0.5,
            'funding_rate': 0.001,
            'volume_24h': 10_000_000
        }
        
        fundamental = {
            'unlock_score': 8.0,
            'market_cap_rank': 150
        }
        
        technical = {
            'rsi': 75.0,  # 超买
            'pattern_score': 8.0  # 看跌形态
        }
        
        sentiment = {
            'social_score': 6.0,
            'news_score': 5.0
        }
        
        score, details = calculate_comprehensive_score(
            contract_data, fundamental, technical, sentiment
        )
        
        assert 0 <= score <= 10
        assert 'contract_data_score' in details
        assert 'fundamental_score' in details
        assert 'technical_score' in details
        assert 'sentiment_score' in details
    
    def test_comprehensive_score_weights(self):
        """测试评分权重"""
        contract_data = {'oi_mc_ratio': 0.3, 'funding_rate': 0.002, 'volume_24h': 50_000_000}
        fundamental = {'unlock_score': 10.0, 'market_cap_rank': 200}
        technical = {'rsi': 80.0, 'pattern_score': 9.0}
        sentiment = {'social_score': 7.0, 'news_score': 6.0}
        
        score, details = calculate_comprehensive_score(
            contract_data, fundamental, technical, sentiment
        )
        
        # 验证权重：合同数据 35%, 基本面 30%, 技术面 25%, 情绪 10%
        assert 'contract_data_score' in details
        assert details['contract_data_score'] * 0.35 + \
               details['fundamental_score'] * 0.30 + \
               details['technical_score'] * 0.25 + \
               details['sentiment_score'] * 0.10 == pytest.approx(score, rel=0.01)
    
    def test_veto_mechanism_oi_ratio(self):
        """测试否决机制 - OI 比率过高"""
        contract_data = {'oi_mc_ratio': 1.5, 'funding_rate': 0.001, 'volume_24h': 10_000_000}
        fundamental = {'unlock_score': 9.0, 'market_cap_rank': 100}
        technical = {'rsi': 70.0, 'pattern_score': 7.0}
        sentiment = {'social_score': 5.0, 'news_score': 5.0}
        
        score, details = calculate_comprehensive_score(
            contract_data, fundamental, technical, sentiment
        )
        
        assert details.get('veto', False) is True
        assert details.get('veto_reason') == 'OI/MC 比率过高 (>1.0)'
    
    def test_veto_mechanism_listing_time(self):
        """测试否决机制 - 上市时间过长"""
        contract_data = {'oi_mc_ratio': 0.5, 'funding_rate': 0.001, 'volume_24h': 10_000_000}
        fundamental = {'unlock_score': 9.0, 'market_cap_rank': 100}
        technical = {'rsi': 70.0, 'pattern_score': 7.0, 'listing_hours': 200}  # 超过 168 小时
        sentiment = {'social_score': 5.0, 'news_score': 5.0}
        
        score, details = calculate_comprehensive_score(
            contract_data, fundamental, technical, sentiment
        )
        
        assert details.get('veto', False) is True
        assert details.get('veto_reason') == '上市时间超过 7 天'
    
    def test_entry_threshold(self):
        """测试入场阈值"""
        contract_data = {'oi_mc_ratio': 0.4, 'funding_rate': 0.002, 'volume_24h': 20_000_000}
        fundamental = {'unlock_score': 9.0, 'market_cap_rank': 180}
        technical = {'rsi': 78.0, 'pattern_score': 8.5}
        sentiment = {'social_score': 6.5, 'news_score': 6.0}
        
        score, details = calculate_comprehensive_score(
            contract_data, fundamental, technical, sentiment
        )
        
        # 应该达到入场阈值 7.0
        assert score >= 7.0 or details.get('veto', False)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
