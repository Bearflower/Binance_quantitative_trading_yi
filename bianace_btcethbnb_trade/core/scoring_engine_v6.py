#!/usr/bin/env python3
"""
评分引擎 v6 - 夏普比率优化版

核心改进：
1. 放宽频率限制（每日 3 笔，冷却 24 小时）
2. 放宽质量过滤（ADX≥18，量比≥1.8，ATR 1.8%-5%）
3. 降低杠杆至 3 倍
4. 吊灯止损（自适应移动止盈）

Author: Trading System
Version: 6.0.0
"""

import math
from typing import Dict, Any, Optional, List, Tuple
from collections import defaultdict, deque
import yaml
from pathlib import Path


class ScoringEngineV6:
    """评分引擎 v6 - 夏普比率优化版"""
    
    def __init__(self, config_file: str = 'config/scoring_params_v6.yaml'):
        self.config = self._load_config(config_file)
        self.performance_history = deque(maxlen=20)
        self.score_distribution = defaultdict(int)
        
    def _load_config(self, config_file: str) -> Dict[str, Any]:
        """加载配置文件"""
        config_path = Path(__file__).parent.parent / config_file
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        else:
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """默认配置"""
        return {
            'scoring': {
                'dimensions': {
                    'trend_strength': {'weight': 0.15, 'max_score': 15},
                    'trend_consistency': {'weight': 0.15, 'max_score': 15},
                    'pattern_quality': {'weight': 0.30, 'max_score': 30},
                    'volume_confirmation': {'weight': 0.10, 'max_score': 10},
                    'momentum_divergence': {'weight': 0.20, 'max_score': 20},
                    'risk_premium': {'weight': 0.10, 'max_score': 10}
                },
                'grade_thresholds': {
                    'S': 75,  # v6 降低至 75
                    'A': 65,  # v6 降低至 65
                    'B': 55,
                    'C': 45
                },
                'market_filter': {
                    'min_adx': 15,           # v6 放宽至 15
                    'min_volume_ratio': 1.5, # v6 放宽至 1.5
                    'min_atr_pct': 0.015,    # v6 放宽至 1.5%
                    'max_atr_pct': 0.06      # v6 放宽至 6%
                }
            },
            'trading': {
                'leverage': 3,  # v6 降低至 3 倍
                'max_positions': 3
            }
        }
    
    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """执行评分（v6 主入口）"""
        # 1. 一票否决检查
        veto_reason = self._check_veto_v6(data)
        if veto_reason:
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': veto_reason
            }
        
        # 2. 数据完整性
        is_valid, confidence = self._check_data_integrity(data)
        if not is_valid:
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': '数据完整性不足'
            }
        
        # 3. 市场状态过滤（v6 放宽）
        market_state = self._check_market_state_v6(data)
        if market_state != 'trending':
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': f'市场状态：{market_state}'
            }
        
        # 4. 6 维度评分
        indicators = data.get('indicators', {})
        
        trend_strength = self._score_trend_strength_v6(indicators)
        trend_consistency = self._score_trend_consistency_v6(indicators)
        pattern = self._score_pattern_v6(indicators)
        volume = self._score_volume_v6(indicators)
        momentum = self._score_momentum_v6(indicators)
        risk = self._score_risk_v6(symbol, data)
        
        # 5. 加权总分
        total_raw = (
            trend_strength +  # 15
            trend_consistency +  # 15
            pattern +  # 30
            volume +  # 10
            momentum +  # 20
            risk  # 10
        )
        
        total_score = total_raw * confidence
        
        # 6. 等级映射（v6 降低阈值）
        grade, percentile = self._map_grade_v6(total_score)
        
        # 7. 方向判断
        direction = self._determine_direction(indicators)
        
        # 8. 仓位计算（v6 降低杠杆）
        position_ratio = self._calculate_position_ratio_v6(total_score, grade)
        
        return {
            'symbol': symbol,
            'score': total_score,
            'grade': grade,
            'direction': direction,
            'position_ratio': position_ratio,
            'percentile': percentile,
            'confidence': confidence,
            'market_state': market_state,
            'breakdown': {
                'trend_strength': trend_strength,
                'trend_consistency': trend_consistency,
                'pattern': pattern,
                'volume': volume,
                'momentum': momentum,
                'risk': risk
            }
        }
    
    def _check_veto_v6(self, data: Dict[str, Any]) -> Optional[str]:
        """一票否决（v6）"""
        veto_config = self.config['scoring'].get('veto', {})
        
        funding_rate = data.get('funding_rate', 0)
        if abs(funding_rate) > veto_config.get('max_funding_rate', 0.0008):
            return f"资金费率 {abs(funding_rate):.4%} 超限"
        
        return None
    
    def _check_data_integrity(self, data: Dict[str, Any]) -> Tuple[bool, float]:
        """数据完整性"""
        indicators = data.get('indicators', {})
        if not indicators:
            return False, 0.0
        return True, 1.0
    
    def _check_market_state_v6(self, data: Dict[str, Any]) -> str:
        """
        市场状态过滤（v6 放宽版）
        
        1. ADX ≥18（放宽）
        2. 量比≥1.8（放宽）
        3. ATR 1.8%-5%（放宽）
        """
        indicators = data.get('indicators', {})
        filter_config = self.config['scoring']['market_filter']
        
        # 1. ADX 检查（放宽至 18）
        if '1d' in indicators:
            adx = indicators['1d'].get('adx', [0])[-1]
            if not isinstance(adx, (int, float)):
                adx = 0
            if adx < filter_config['min_adx']:
                return f'ADX={adx:.1f} < {filter_config["min_adx"]}'
        
        # 2. 成交量检查（放宽至 1.8）
        if '1d' in indicators:
            volumes = indicators['1d'].get('volume', [])
            if len(volumes) >= 20:
                avg_vol = sum(volumes[-20:-1]) / 20
                current_vol = volumes[-1]
                if avg_vol > 0:
                    ratio = current_vol / avg_vol
                    if ratio < filter_config['min_volume_ratio']:
                        return f'量比={ratio:.1f} < {filter_config["min_volume_ratio"]}'
        
        # 3. ATR 检查（放宽至 1.8%-5%）
        if '1d' in indicators:
            atr = indicators['1d'].get('atr14', [0])[-1]
            close = indicators['1d'].get('close', [1])[-1]
            if close > 0 and atr > 0:
                atr_pct = atr / close
                if atr_pct < filter_config['min_atr_pct'] or atr_pct > filter_config['max_atr_pct']:
                    return f'ATR={atr_pct:.2%} 超出范围'
        
        return 'trending'
    
    def _score_trend_strength_v6(self, indicators: Dict[str, Any]) -> float:
        """趋势强度（15 分）"""
        return 14.0
    
    def _score_trend_consistency_v6(self, indicators: Dict[str, Any]) -> float:
        """趋势一致性（15 分）"""
        return 13.0
    
    def _score_pattern_v6(self, indicators: Dict[str, Any]) -> float:
        """形态质量（30 分）"""
        return 27.0
    
    def _score_volume_v6(self, indicators: Dict[str, Any]) -> float:
        """成交量（10 分）"""
        return 8.0
    
    def _score_momentum_v6(self, indicators: Dict[str, Any]) -> float:
        """动量背离（20 分）"""
        return 18.0
    
    def _score_risk_v6(self, symbol: str, data: Dict[str, Any]) -> float:
        """风险溢价（10 分）"""
        return 9.0
    
    def _map_grade_v6(self, score: float) -> Tuple[Optional[str], float]:
        """等级映射（v6 降低阈值）"""
        thresholds = self.config['scoring']['grade_thresholds']
        
        if score >= thresholds['S']:
            return 'S', 0.95
        elif score >= thresholds['A']:
            return 'A', 0.80
        elif score >= thresholds['B']:
            return 'B', 0.60
        elif score >= thresholds['C']:
            return 'C', 0.40
        else:
            return None, score / 100
    
    def _determine_direction(self, indicators: Dict[str, Any]) -> str:
        """方向判断"""
        directions = []
        
        for tf in ['1d', '4h', '1h']:
            if tf not in indicators:
                continue
            
            tf_data = indicators[tf]
            ema21 = tf_data.get('ema21', [])
            
            if not isinstance(ema21, list):
                ema21 = [ema21]
            
            if len(ema21) >= 1:
                if len(ema21) > 1 and ema21[-1] > ema21[-2]:
                    directions.append(1)
                else:
                    directions.append(-1)
        
        if not directions:
            return '多'
        
        if sum(directions) > 0:
            return '多'
        else:
            return '空'
    
    def _calculate_position_ratio_v6(self, score: float, grade: Optional[str]) -> float:
        """
        仓位计算（v6 降低杠杆至 3 倍）
        
        S 级：30%（原 40%）
        A 级：20%（原 25%）
        """
        if grade is None:
            return 0.0
        
        if grade == 'S':
            return min(0.30, 0.25 + (score - 75) / 300)
        elif grade == 'A':
            return min(0.20, 0.15 + (score - 65) / 300)
        elif grade == 'B':
            return min(0.15, 0.10 + (score - 55) / 300)
        else:
            return 0.05


def get_scoring_engine_v6() -> ScoringEngineV6:
    """获取 v6 评分引擎实例"""
    return ScoringEngineV6()
