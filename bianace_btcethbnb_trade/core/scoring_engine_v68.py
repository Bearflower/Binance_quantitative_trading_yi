#!/usr/bin/env python3
"""
评分引擎 v6.8 - 务实融合版

核心改进（相比 v6.7）：
1. 取消所有前置过滤器（ADX、成交量、ATR 硬性门槛）
2. 新增 B 级（≥50 分）和 C 级（≥40 分）
3. 降低 S 级阈值：75 → 70
4. 降低 A 级阈值：65 → 60
5. 超轻仓档位：B 级 15%、C 级 5%

目标：
- 交易数：80-120 笔/6 个月
- 夏普比率：≥0.5
- 净利润：+80-120%

Author: Trading System
Version: 6.8.0
"""

import math
from typing import Dict, Any, Optional, List, Tuple
from collections import defaultdict, deque
import yaml
from pathlib import Path


class ScoringEngineV68:
    """评分引擎 v6.8 - 务实融合版"""
    
    def __init__(self, config_file: str = 'config/scoring_params_v68.yaml'):
        self.config = self._load_config(config_file)
        self.performance_history = deque(maxlen=20)
        
    def _load_config(self, config_file: str) -> Dict[str, Any]:
        """加载配置文件"""
        config_path = Path(__file__).parent.parent / config_file
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        else:
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """默认配置（v6.8）"""
        return {
            'scoring': {
                'dimensions': {
                    'trend_strength': {'weight': 0.15, 'max_score': 15},
                    'trend_consistency': {'weight': 0.15, 'max_score': 15},
                    'pattern_quality': {'weight': 0.25, 'max_score': 25},
                    'volume_confirmation': {'weight': 0.15, 'max_score': 15},
                    'momentum_divergence': {'weight': 0.20, 'max_score': 20},
                    'risk_premium': {'weight': 0.10, 'max_score': 10}
                },
                'grade_thresholds': {
                    'S': 70,   # v6.8: 75 → 70
                    'A': 60,   # v6.8: 65 → 60
                    'B': 50,   # v6.8: 新增
                    'C': 40    # v6.8: 新增
                },
                'position_mapping': {
                    'S': 0.40,
                    'A': 0.30,
                    'B': 0.15,  # v6.8: 新增
                    'C': 0.05   # v6.8: 新增
                },
                'leverage': {
                    'S': 4,
                    'A': 3,
                    'B': 2,
                    'C': 2
                }
            },
            'trading': {
                'base_position_s': 0.40,
                'base_position_a': 0.30,
                'base_position_b': 0.15,
                'base_position_c': 0.05
            }
        }
    
    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """执行评分（v6.8 主入口）"""
        # 1. 一票否决检查（仅保留资金费率和极端波动）
        veto_reason = self._check_veto(data)
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
        
        # 3. 基础趋势过滤器（仅检查方向，不再硬性门槛）
        trend_ok, trend_reason = self._check_trend_filter_v68(data)
        if not trend_ok:
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': f'趋势过滤：{trend_reason}'
            }
        
        # v6.8: 取消所有前置过滤器（ADX、成交量、ATR）
        
        # 4. 6 维度评分
        indicators = data.get('indicators', {})
        
        trend_strength = self._score_trend_strength(indicators)
        trend_consistency = self._score_trend_consistency(indicators)
        pattern = self._score_pattern_v68(indicators)
        volume = self._score_volume(indicators)
        momentum = self._score_momentum(indicators)
        risk = self._score_risk_v68(symbol, data)
        
        # 加权总分
        total_score = (
            trend_strength +
            trend_consistency +
            pattern +
            volume +
            momentum +
            risk
        )
        
        # 5. 确定等级（v6.8: 新增 B/C 级）
        grade = self._determine_grade_v68(total_score)
        if grade is None:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0
            }
        
        # 6. 确定方向
        direction = self._determine_direction(indicators)
        if direction is None:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': grade,
                'direction': None,
                'position_ratio': 0.0
            }
        
        # 7. 计算仓位
        position_ratio = self._calculate_position(grade, total_score, data)
        
        return {
            'symbol': symbol,
            'score': total_score,
            'grade': grade,
            'direction': direction,
            'position_ratio': position_ratio,
            'breakdown': {
                'trend_strength': trend_strength,
                'trend_consistency': trend_consistency,
                'pattern': pattern,
                'volume': volume,
                'momentum': momentum,
                'risk': risk
            }
        }
    
    def _check_veto(self, data: Dict[str, Any]) -> Optional[str]:
        """一票否决检查（简化）"""
        funding_rate = data.get('funding_rate', 0.0001)
        if abs(funding_rate) > 0.001:  # v6.8: 放宽至 0.1%
            return f'资金费率={funding_rate:.4%}'
        
        price_change = data.get('price_change_24h', 0.0)
        if abs(price_change) > 0.20:  # v6.8: 放宽至 20%
            return f'24h 涨跌幅={price_change:.2%}'
        
        return None
    
    def _check_data_integrity(self, data: Dict[str, Any]) -> Tuple[bool, float]:
        """数据完整性检查"""
        indicators = data.get('indicators', {})
        
        required_count = 0
        valid_count = 0
        
        for tf in ['1d', '4h', '1h']:
            if tf in indicators:
                required_count += 1
                tf_data = indicators[tf]
                if tf_data.get('ema21') and tf_data.get('macd'):
                    valid_count += 1
        
        confidence = valid_count / required_count if required_count > 0 else 0.0
        return valid_count >= 2, confidence
    
    def _check_trend_filter_v68(self, data: Dict[str, Any]) -> Tuple[bool, str]:
        """趋势过滤器 v6.8（简化版，仅检查 1d）"""
        indicators = data.get('indicators', {})
        
        if '1d' not in indicators:
            return False, '无 1d 数据'
        
        tf_data = indicators['1d']
        closes = tf_data.get('close', [])
        ema21 = tf_data.get('ema21', [])
        ema55 = tf_data.get('ema55', [])
        
        if not isinstance(closes, list): closes = [closes]
        if not isinstance(ema21, list): ema21 = [ema21]
        if not isinstance(ema55, list): ema55 = [ema55]
        
        if len(closes) < 2 or len(ema21) < 2 or len(ema55) < 1:
            return False, '数据不足'
        
        current_close = closes[-1]
        current_ema21 = ema21[-1]
        current_ema55 = ema55[-1]
        prev_ema21 = ema21[-2]
        
        # 简化判断：仅检查收盘价和 EMA 关系
        is_long = (
            current_close > current_ema55 and
            current_ema21 > prev_ema21
        )
        
        is_short = (
            current_close < current_ema55 and
            current_ema21 < prev_ema21
        )
        
        if not is_long and not is_short:
            return False, '趋势不明确'
        
        # v6.8: 取消 ATR 硬性检查，改为纳入评分
        
        return True, '多头' if is_long else '空头'
    
    def _determine_grade_v68(self, score: float) -> Optional[str]:
        """确定等级 v6.8（新增 B/C 级）"""
        thresholds = self.config['scoring']['grade_thresholds']
        
        if score >= thresholds['S']:
            return 'S'
        elif score >= thresholds['A']:
            return 'A'
        elif score >= thresholds['B']:
            return 'B'
        elif score >= thresholds['C']:
            return 'C'
        else:
            return None
    
    def _determine_direction(self, indicators: Dict[str, Any]) -> Optional[str]:
        """确定方向"""
        directions = []
        
        for tf in ['1d', '4h', '1h']:
            if tf not in indicators:
                continue
            tf_data = indicators[tf]
            ema21 = tf_data.get('ema21', [])
            
            if not isinstance(ema21, list):
                ema21 = [ema21]
            
            if len(ema21) >= 2:
                if ema21[-1] > ema21[-2]:
                    directions.append(1)
                else:
                    directions.append(-1)
        
        if not directions:
            return None
        
        if sum(directions) > 0:
            return '多'
        elif sum(directions) < 0:
            return '空'
        else:
            return None
    
    def _calculate_position(self, grade: str, score: float, data: Dict[str, Any]) -> float:
        """计算仓位"""
        config = self.config['scoring']
        
        # 基础仓位
        base_position = config['position_mapping'].get(grade, 0.0)
        
        # 分数调整
        score_factor = min(1.0, (score - 40) / 40)
        score_factor = max(0.8, score_factor)
        
        # 波动率调整
        vol_adjustment = self._calculate_volatility_target(data)
        
        position_ratio = base_position * score_factor * vol_adjustment
        position_ratio = min(0.50, max(0.0, position_ratio))
        
        return position_ratio
    
    def _calculate_volatility_target(self, data: Dict[str, Any]) -> float:
        """波动率目标调整"""
        indicators = data.get('indicators', {})
        if '1d' not in indicators:
            return 1.0
        
        atr = indicators['1d'].get('atr14', [0])[-1]
        close = indicators['1d'].get('close', [1])[-1]
        
        if close == 0 or atr == 0:
            return 1.0
        
        current_atr_pct = atr / close
        
        median_atr = {
            'BTCUSDT': 0.028,
            'ETHUSDT': 0.032,
            'BNBUSDT': 0.050
        }.get('BTCUSDT', 0.03)
        
        vol_adjustment = median_atr / current_atr_pct
        vol_adjustment = max(0.5, min(1.5, vol_adjustment))
        
        return vol_adjustment
    
    # ========== 6 维度评分函数 ==========
    
    def _score_trend_strength(self, indicators: Dict[str, Any]) -> float:
        """趋势强度（0-15）"""
        score = 7.5
        
        ema_count = 0
        for tf in ['1d', '4h', '1h']:
            if tf not in indicators:
                continue
            tf_data = indicators[tf]
            ema21 = tf_data.get('ema21', [])
            ema50 = tf_data.get('ema50', [])
            
            if not isinstance(ema21, list): ema21 = [ema21]
            if not isinstance(ema50, list): ema50 = [ema50]
            
            if len(ema21) >= 1 and len(ema50) >= 1:
                if ema21[-1] > ema50[-1]:
                    ema_count += 1
        
        if ema_count == 3:
            score = 14.0
        elif ema_count == 2:
            score = 10.0
        elif ema_count == 1:
            score = 6.0
        else:
            score = 3.0
        
        return score
    
    def _score_trend_consistency(self, indicators: Dict[str, Any]) -> float:
        """趋势一致性（0-15）"""
        directions = []
        
        for tf in ['1d', '4h', '1h']:
            if tf not in indicators:
                continue
            tf_data = indicators[tf]
            ema21 = tf_data.get('ema21', [])
            
            if not isinstance(ema21, list): ema21 = [ema21]
            
            if len(ema21) >= 2:
                if ema21[-1] > ema21[-2]:
                    directions.append(1)
                else:
                    directions.append(-1)
        
        if len(directions) >= 2:
            if all(d == directions[0] for d in directions):
                return 14.0
            elif directions.count(1) == 2 or directions.count(-1) == 2:
                return 10.0
            else:
                return 6.0
        
        return 7.5
    
    def _score_pattern_v68(self, indicators: Dict[str, Any]) -> float:
        """形态质量 v6.8（0-25）"""
        if '1d' not in indicators:
            return 12.5
        
        tf_data = indicators['1d']
        closes = tf_data.get('close', [])
        ema21 = tf_data.get('ema21', [])
        ema50 = tf_data.get('ema50', [])
        
        if not isinstance(closes, list): closes = [closes]
        if not isinstance(ema21, list): ema21 = [ema21]
        if not isinstance(ema50, list): ema50 = [ema50]
        
        if len(closes) < 5 or len(ema21) < 1 or len(ema50) < 1:
            return 12.5
        
        score = 12.5
        
        # EMA 排列（+5）
        if ema21[-1] > ema50[-1]:
            score += 5.0
        
        # 价格位置（+5）
        if closes[-1] > ema21[-1]:
            score += 5.0
        
        # 近期涨幅（+5）
        if len(closes) >= 5:
            change = (closes[-1] - closes[-5]) / closes[-5] * 100
            if 0 < change < 8:
                score += 5.0
            elif -8 < change < 0:
                score += 5.0
            elif abs(change) >= 8:
                score -= 5.0
        
        return max(0.0, min(25.0, score))
    
    def _score_volume(self, indicators: Dict[str, Any]) -> float:
        """成交量（0-15）"""
        if '1d' not in indicators:
            return 7.5
        
        volumes = indicators['1d'].get('volume', [])
        if len(volumes) < 20:
            return 7.5
        
        avg_vol = sum(volumes[-20:-1]) / 20
        current_vol = volumes[-1]
        
        if avg_vol == 0:
            return 7.5
        
        ratio = current_vol / avg_vol
        
        if ratio >= 2.5:
            return 14.5
        elif ratio >= 2.0:
            return 12.5
        elif ratio >= 1.8:
            return 10.5
        elif ratio >= 1.6:
            return 9.0
        elif ratio >= 1.3:
            return 7.5
        else:
            return 6.0
    
    def _score_momentum(self, indicators: Dict[str, Any]) -> float:
        """动量背离（0-20）"""
        if '1d' not in indicators:
            return 10.0
        
        macd_hist = indicators['1d'].get('macd_hist', [])
        if len(macd_hist) < 3:
            return 10.0
        
        if macd_hist[-1] > 0 and macd_hist[-1] > macd_hist[-2] > macd_hist[-3]:
            return 18.0
        elif macd_hist[-1] < 0 and macd_hist[-1] < macd_hist[-2] < macd_hist[-3]:
            return 18.0
        elif macd_hist[-1] * macd_hist[-2] < 0:
            return 13.0
        else:
            return 10.0
    
    def _score_risk_v68(self, symbol: str, data: Dict[str, Any]) -> float:
        """风险溢价 v6.8（0-10）"""
        score = 6.0
        
        funding_rate = data.get('funding_rate', 0.0001)
        if abs(funding_rate) > 0.001:
            score -= 3.0
        elif abs(funding_rate) < 0.00005:
            score += 2.0
        
        price_change = data.get('price_change_24h', 0.0)
        if abs(price_change) < 0.05:
            score += 2.0
        elif abs(price_change) > 0.12:
            score -= 3.0
        
        return max(0.0, min(10.0, score))


def get_scoring_engine_v68() -> ScoringEngineV68:
    """获取 v6.8 评分引擎实例"""
    return ScoringEngineV68()
