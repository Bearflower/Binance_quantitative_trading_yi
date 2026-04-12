#!/usr/bin/env python3
"""
评分引擎 v6.9 - 简化务实版

核心改进（相比 v6.8）：
1. 简化评分系统：回归 v6 原版逻辑（减少分数虚高）
2. 保留 B 级（≥50 分）和 C 级（≥40 分）
3. 保留宽松频率控制（6 笔/天，8h 冷却）
4. 强化趋势过滤器（必须 1d+4h 同向）

问题诊断（v6.8 失败原因）：
- 6-dimension 评分系统分数虚高，区分度不足
- pattern_quality 基础分 12.5，轻易拿到 17-22 分
- risk_premium 基础分 6.0，通常拿到 8 分
- 导致 40-60 分信号过多，但质量无法保证

解决方案：
- 简化为 3 维度评分（趋势、形态、成交量）
- 降低基础分，增加区分度
- 强化趋势过滤器（1d+4h 同向）

目标：
- 交易数：80-120 笔/6 个月
- 夏普比率：≥0.5
- 胜率：52-58%

Author: Trading System
Version: 6.9.0
"""

import math
from typing import Dict, Any, Optional, List, Tuple
from collections import defaultdict, deque
import yaml
from pathlib import Path


class ScoringEngineV69:
    """评分引擎 v6.9 - 简化务实版"""
    
    def __init__(self, config_file: str = 'config/scoring_params_v69.yaml'):
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
        """默认配置（v6.9 简化版）"""
        return {
            'scoring': {
                'dimensions': {
                    'trend_quality': {'weight': 0.40, 'max_score': 40},
                    'pattern_quality': {'weight': 0.35, 'max_score': 35},
                    'volume_confirmation': {'weight': 0.25, 'max_score': 25}
                },
                'grade_thresholds': {
                    'S': 70,
                    'A': 60,
                    'B': 50,
                    'C': 40
                },
                'position_mapping': {
                    'S': 0.40,
                    'A': 0.30,
                    'B': 0.15,
                    'C': 0.05
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
        """执行评分（v6.9 主入口）"""
        # 1. 一票否决检查
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
        
        # 3. 强化趋势过滤器（v6.9: 必须 1d+4h 同向）
        trend_ok, trend_reason = self._check_trend_filter_v69(data)
        if not trend_ok:
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': f'趋势过滤：{trend_reason}'
            }
        
        # 4. 3 维度评分（简化版）
        indicators = data.get('indicators', {})
        
        trend_score = self._score_trend_quality_v69(indicators)
        pattern_score = self._score_pattern_v69(indicators)
        volume_score = self._score_volume_v69(indicators)
        
        total_score = trend_score + pattern_score + volume_score
        
        # 5. 确定等级
        grade = self._determine_grade_v69(total_score)
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
                'trend_quality': trend_score,
                'pattern_quality': pattern_score,
                'volume_confirmation': volume_score
            }
        }
    
    def _check_veto(self, data: Dict[str, Any]) -> Optional[str]:
        """一票否决检查"""
        funding_rate = data.get('funding_rate', 0.0001)
        if abs(funding_rate) > 0.001:
            return f'资金费率={funding_rate:.4%}'
        
        price_change = data.get('price_change_24h', 0.0)
        if abs(price_change) > 0.20:
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
    
    def _check_trend_filter_v69(self, data: Dict[str, Any]) -> Tuple[bool, str]:
        """趋势过滤器 v6.9（强化版：必须 1d+4h 同向）"""
        indicators = data.get('indicators', {})
        
        if '1d' not in indicators or '4h' not in indicators:
            return False, '无 1d 或 4h 数据'
        
        # 检查 1d 趋势
        tf_data_1d = indicators['1d']
        closes_1d = tf_data_1d.get('close', [])
        ema21_1d = tf_data_1d.get('ema21', [])
        ema55_1d = tf_data_1d.get('ema55', [])
        
        if not isinstance(closes_1d, list): closes_1d = [closes_1d]
        if not isinstance(ema21_1d, list): ema21_1d = [ema21_1d]
        if not isinstance(ema55_1d, list): ema55_1d = [ema55_1d]
        
        if len(closes_1d) < 2 or len(ema21_1d) < 2 or len(ema55_1d) < 1:
            return False, '数据不足'
        
        # 1d 多头条件
        is_long_1d = (
            closes_1d[-1] > ema55_1d[-1] and
            ema21_1d[-1] > ema21_1d[-2]
        )
        
        # 1d 空头条件
        is_short_1d = (
            closes_1d[-1] < ema55_1d[-1] and
            ema21_1d[-1] < ema21_1d[-2]
        )
        
        # 检查 4h 趋势
        tf_data_4h = indicators['4h']
        ema21_4h = tf_data_4h.get('ema21', [])
        
        if not isinstance(ema21_4h, list): ema21_4h = [ema21_4h]
        
        if len(ema21_4h) < 2:
            return False, '4h 数据不足'
        
        # 4h 多头条件
        is_long_4h = ema21_4h[-1] > ema21_4h[-2]
        
        # 4h 空头条件
        is_short_4h = ema21_4h[-1] < ema21_4h[-2]
        
        # v6.9: 必须 1d+4h 同向
        is_long = is_long_1d and is_long_4h
        is_short = is_short_1d and is_short_4h
        
        if not is_long and not is_short:
            return False, '1d+4h 趋势不同向'
        
        return True, '多头' if is_long else '空头'
    
    def _determine_grade_v69(self, score: float) -> Optional[str]:
        """确定等级 v6.9"""
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
        
        base_position = config['position_mapping'].get(grade, 0.0)
        
        score_factor = min(1.0, (score - 40) / 40)
        score_factor = max(0.8, score_factor)
        
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
        
        symbol = data.get('symbol', 'BTCUSDT')
        median_atr = {
            'BTCUSDT': 0.028,
            'ETHUSDT': 0.032,
            'BNBUSDT': 0.050
        }.get(symbol, 0.03)
        
        vol_adjustment = median_atr / current_atr_pct
        vol_adjustment = max(0.5, min(1.5, vol_adjustment))
        
        return vol_adjustment
    
    # ========== 3 维度评分函数（简化版） ==========
    
    def _score_trend_quality_v69(self, indicators: Dict[str, Any]) -> float:
        """趋势质量（0-40）- v6.9 核心维度"""
        if '1d' not in indicators:
            return 20.0
        
        tf_data = indicators['1d']
        closes = tf_data.get('close', [])
        ema21 = tf_data.get('ema21', [])
        ema55 = tf_data.get('ema55', [])
        
        if not isinstance(closes, list): closes = [closes]
        if not isinstance(ema21, list): ema21 = [ema21]
        if not isinstance(ema55, list): ema55 = [ema55]
        
        if len(closes) < 2 or len(ema21) < 2 or len(ema55) < 1:
            return 20.0
        
        score = 20.0  # 基础分
        
        # EMA55 方向（+10）
        if ema55[-1] > ema55[-2] if len(ema55) >= 2 else True:
            score += 10.0
        else:
            score -= 5.0
        
        # EMA21 斜率（+10）
        ema21_slope = (ema21[-1] - ema21[-2]) / ema21[-2] * 100 if ema21[-2] != 0 else 0
        if ema21_slope > 0.5:
            score += 10.0
        elif ema21_slope > 0:
            score += 5.0
        elif ema21_slope < -0.5:
            score -= 10.0
        else:
            score -= 5.0
        
        # 价格相对 EMA55 位置（+10）
        price_position = (closes[-1] - ema55[-1]) / ema55[-1] * 100 if ema55[-1] != 0 else 0
        if 0 < price_position < 5:
            score += 10.0  # 刚突破，最佳
        elif price_position >= 5:
            score += 5.0  # 已远离，次佳
        elif -5 < price_position <= 0:
            score += 7.5  # 回踩，可接受
        else:
            score -= 5.0  # 深度回调，避免
        
        return max(0.0, min(40.0, score))
    
    def _score_pattern_v69(self, indicators: Dict[str, Any]) -> float:
        """形态质量（0-35）- v6.9 简化版"""
        if '1d' not in indicators:
            return 17.5
        
        tf_data = indicators['1d']
        closes = tf_data.get('close', [])
        ema21 = tf_data.get('ema21', [])
        ema50 = tf_data.get('ema50', [])
        
        if not isinstance(closes, list): closes = [closes]
        if not isinstance(ema21, list): ema21 = [ema21]
        if not isinstance(ema50, list): ema50 = [ema50]
        
        if len(closes) < 5 or len(ema21) < 1 or len(ema50) < 1:
            return 17.5
        
        score = 15.0  # 降低基础分
        
        # EMA 排列（+10）
        if ema21[-1] > ema50[-1]:
            score += 10.0
        else:
            score -= 5.0
        
        # 价格位置（+10）
        if closes[-1] > ema21[-1]:
            score += 10.0
        else:
            score -= 5.0
        
        # 近期涨幅（+5）
        if len(closes) >= 5:
            change = (closes[-1] - closes[-5]) / closes[-5] * 100
            if 0 < change < 8:
                score += 5.0
            elif -8 < change <= 0:
                score += 3.0
            elif abs(change) >= 8:
                score -= 5.0
        
        return max(0.0, min(35.0, score))
    
    def _score_volume_v69(self, indicators: Dict[str, Any]) -> float:
        """成交量（0-25）- v6.9 简化版"""
        if '1d' not in indicators:
            return 12.5
        
        volumes = indicators['1d'].get('volume', [])
        if len(volumes) < 20:
            return 12.5
        
        avg_vol = sum(volumes[-20:-1]) / 20
        current_vol = volumes[-1]
        
        if avg_vol == 0:
            return 12.5
        
        ratio = current_vol / avg_vol
        
        if ratio >= 2.5:
            return 24.0
        elif ratio >= 2.0:
            return 20.0
        elif ratio >= 1.8:
            return 17.0
        elif ratio >= 1.5:
            return 14.0
        elif ratio >= 1.2:
            return 12.0
        else:
            return 8.0


def get_scoring_engine_v69() -> ScoringEngineV69:
    """获取 v6.9 评分引擎实例"""
    return ScoringEngineV69()
