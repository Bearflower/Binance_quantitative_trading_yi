#!/usr/bin/env python3
"""
评分引擎 v7.6 - 前置过滤器放宽版

核心改进（相比 v7.5）：
1. ADX 最小值：20 → 15（允许弱趋势市场）
2. 成交量倍数（S 级）：1.8 → 1.5
3. 成交量倍数（A 级）：1.5 → 1.2
4. ATR/价格上限：5% → 6%

目标：
- 交易数：80-120 笔/6 个月
- 夏普比率：≥0.7
- 胜率：55-60%

Author: Trading System
Version: 7.6.0
"""

import math
from typing import Dict, Any, Optional, List
from collections import deque
import yaml
from pathlib import Path


class ScoringEngineV76:
    """v7.6 放宽版评分引擎"""
    
    def __init__(self, config_file: str = 'config/scoring_params_v76.yaml'):
        self.config = self._load_config(config_file)
        self.signal_history = deque(maxlen=200)
        
    def _load_config(self, config_file: str) -> Dict[str, Any]:
        """加载配置文件"""
        config_path = Path(__file__).parent.parent / config_file
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        return self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """默认配置（v7.6 放宽版）"""
        return {
            'scoring': {
                'top_percent': 0.35,  # Top 35%
                'min_score_to_trade': 40,  # 最低 40 分
                'position_mapping': {
                    '80': 1.0,
                    '70': 0.8,
                    '60': 0.6,
                    '50': 0.4,
                    '40': 0.2
                },
                'base_position': {
                    'S': 0.35,
                    'A': 0.25
                }
            },
            'exit': {
                'stop_atr': 1.2,
                'target1_atr': 3.5,
                'target2_atr': 5.5,
                'chandelier_atr': 1.5,
                'chandelier_trigger': 2.5
            },
            'filters': {
                'min_adx': 15,  # v7.6: 20 → 15
                'min_volume_ratio_s': 1.5,  # v7.6: 1.8 → 1.5
                'min_volume_ratio_a': 1.2,  # v7.6: 1.5 → 1.2
                'max_atr_pct': 0.06,  # v7.6: 5% → 6%
                'min_atr_pct': 0.015  # 保持 1.5%
            }
        }
    
    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        v7.6 主评分入口
        
        1. 6 维度连续评分
        2. 信号排序（Top 35%）
        3. 分段仓位映射
        4. 波动率调整
        """
        # 1. 检查市场状态过滤（v7.6 放宽）
        if not self._check_market_filters(data):
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'reason': '市场过滤未通过'
            }
        
        # 2. 6 维度连续评分
        indicators = data.get('indicators', {})
        
        trend_strength = self._score_trend_strength(indicators)
        trend_consistency = self._score_trend_consistency(indicators)
        pattern = self._score_pattern(indicators)
        volume = self._score_volume(indicators)
        momentum = self._score_momentum(indicators)
        risk = self._score_risk(symbol, data)
        
        # 加权总分
        total_score = (
            trend_strength * 0.15 +
            trend_consistency * 0.15 +
            pattern * 0.25 +
            volume * 0.15 +
            momentum * 0.20 +
            risk * 0.10
        )
        
        # 3. 记录信号历史
        self.signal_history.append({
            'symbol': symbol,
            'score': total_score,
            'time': data.get('timestamp', '')
        })
        
        # 4. 计算百分位
        percentile = self._calculate_percentile(total_score)
        
        # 5. 检查是否在前 35%
        top_percent = self.config['scoring']['top_percent']
        if percentile < top_percent:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'percentile': percentile,
                'reason': f'排名={percentile:.1%} < {top_percent:.0%}'
            }
        
        # 6. 检查最低分数（40 分）
        min_score = self.config['scoring']['min_score_to_trade']
        if total_score < min_score:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'percentile': percentile
            }
        
        # 7. 确定方向
        direction = self._determine_direction(indicators)
        if direction is None:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'percentile': percentile
            }
        
        # 8. 分段仓位映射
        position_coefficient = self._get_position_coefficient(total_score)
        
        # 9. 基础仓位
        base_coefficient = self.config['scoring']['base_position']['S'] if total_score >= 80 else self.config['scoring']['base_position']['A']
        
        # 10. 百分位微调
        percentile_bonus = 1.1 if percentile > 0.90 else 1.0
        
        # 11. 波动率调整
        vol_adjustment = self._calculate_volatility_target(symbol, data)
        
        # 12. 最终仓位
        position_ratio = base_coefficient * position_coefficient * percentile_bonus * vol_adjustment
        position_ratio = min(0.50, max(0.0, position_ratio))
        
        # 13. 确定等级
        if total_score >= 80:
            grade = 'S'
        elif total_score >= 70:
            grade = 'A'
        elif total_score >= 60:
            grade = 'B'
        elif total_score >= 50:
            grade = 'C'
        else:
            grade = 'D'
        
        return {
            'symbol': symbol,
            'score': total_score,
            'grade': grade,
            'direction': direction,
            'position_ratio': position_ratio,
            'percentile': percentile,
            'vol_adjustment': vol_adjustment,
            'breakdown': {
                'trend_strength': trend_strength,
                'trend_consistency': trend_consistency,
                'pattern': pattern,
                'volume': volume,
                'momentum': momentum,
                'risk': risk
            }
        }
    
    def _check_market_filters(self, data: Dict[str, Any]) -> bool:
        """市场过滤（v7.6 放宽版：ADX≥15、量比≥1.5、ATR≤6%）"""
        indicators = data.get('indicators', {})
        filters = self.config['filters']
        
        # ADX 检查（v7.6: ≥15）
        if '1d' in indicators:
            adx = indicators['1d'].get('adx', [0])[-1]
            if not isinstance(adx, (int, float)):
                adx = 0
            if adx < filters['min_adx']:
                return False
        
        # 量比检查（v7.6: S 级≥1.5, A 级≥1.2）
        if '1d' in indicators:
            volumes = indicators['1d'].get('volume', [])
            if len(volumes) >= 20:
                avg_vol = sum(volumes[-20:-1]) / 20
                current_vol = volumes[-1]
                if avg_vol > 0:
                    ratio = current_vol / avg_vol
                    # v7.6: 使用较宽松的 S 级标准 1.5
                    if ratio < filters['min_volume_ratio_s']:
                        return False
        
        # ATR 检查（v7.6: ≤6%）
        if '1d' in indicators:
            atr = indicators['1d'].get('atr14', [0])[-1]
            close = indicators['1d'].get('close', [1])[-1]
            if close > 0 and atr > 0:
                atr_pct = atr / close
                if atr_pct > filters['max_atr_pct']:
                    return False
                # 检查低波动率（保持≥1.5%）
                if atr_pct < filters['min_atr_pct']:
                    return False
        
        return True
    
    def _calculate_percentile(self, score: float) -> float:
        """计算百分位"""
        if len(self.signal_history) == 0:
            return 1.0
        
        scores = [s['score'] for s in self.signal_history]
        rank = sum(1 for s in scores if s < score)
        return rank / len(scores)
    
    def _get_position_coefficient(self, score: float) -> float:
        """
        分段线性仓位映射
        
        ≥80: 1.0
        70-79: 0.8
        60-69: 0.6
        50-59: 0.4
        40-49: 0.2
        <40: 0.0
        """
        config = self.config['scoring']['position_mapping']
        
        if score >= 80:
            return float(config['80'])
        elif score >= 70:
            return float(config['70'])
        elif score >= 60:
            return float(config['60'])
        elif score >= 50:
            return float(config['50'])
        elif score >= 40:
            return float(config['40'])
        else:
            return 0.0
    
    def _calculate_volatility_target(self, symbol: str, data: Dict[str, Any]) -> float:
        """波动率目标调整"""
        indicators = data.get('indicators', {})
        if '1d' not in indicators:
            return 1.0
        
        atr = indicators['1d'].get('atr14', [0])[-1]
        close = indicators['1d'].get('close', [1])[-1]
        
        if close == 0 or atr == 0:
            return 1.0
        
        current_atr_pct = atr / close
        
        # 各币种中位数 ATR%
        median_atr = {
            'BTCUSDT': 0.028,
            'ETHUSDT': 0.032,
            'BNBUSDT': 0.050
        }.get(symbol, 0.03)
        
        vol_adjustment = median_atr / current_atr_pct
        vol_adjustment = max(0.5, min(1.5, vol_adjustment))
        
        return vol_adjustment
    
    def _determine_direction(self, indicators: Dict[str, Any]) -> Optional[str]:
        """方向判断"""
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
    
    # ========== 6 维度评分函数 ==========
    
    def _score_trend_strength(self, indicators: Dict[str, Any]) -> float:
        """趋势强度（0-100）"""
        score = 50.0
        
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
            score = 90.0
        elif ema_count == 2:
            score = 70.0
        elif ema_count == 1:
            score = 50.0
        else:
            score = 30.0
        
        return score
    
    def _score_trend_consistency(self, indicators: Dict[str, Any]) -> float:
        """趋势一致性（0-100）"""
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
                return 90.0
            elif directions.count(1) == 2 or directions.count(-1) == 2:
                return 70.0
            else:
                return 40.0
        
        return 50.0
    
    def _score_pattern(self, indicators: Dict[str, Any]) -> float:
        """形态质量（0-100）"""
        return 65.0
    
    def _score_volume(self, indicators: Dict[str, Any]) -> float:
        """成交量（0-100）"""
        if '1d' not in indicators:
            return 50.0
        
        volumes = indicators['1d'].get('volume', [])
        if len(volumes) < 20:
            return 50.0
        
        avg_vol = sum(volumes[-20:-1]) / 20
        current_vol = volumes[-1]
        
        if avg_vol == 0:
            return 50.0
        
        ratio = current_vol / avg_vol
        
        if ratio >= 2.5:
            return 95.0
        elif ratio >= 2.0:
            return 85.0
        elif ratio >= 1.8:
            return 75.0
        elif ratio >= 1.5:
            return 65.0
        elif ratio >= 1.2:
            return 55.0
        else:
            return 45.0
    
    def _score_momentum(self, indicators: Dict[str, Any]) -> float:
        """动量背离（0-100）"""
        if '1d' not in indicators:
            return 50.0
        
        macd_hist = indicators['1d'].get('macd_hist', [])
        if len(macd_hist) < 3:
            return 50.0
        
        if macd_hist[-1] > 0 and macd_hist[-1] > macd_hist[-2] > macd_hist[-3]:
            return 85.0
        elif macd_hist[-1] < 0 and macd_hist[-1] < macd_hist[-2] < macd_hist[-3]:
            return 85.0
        elif macd_hist[-1] * macd_hist[-2] < 0:
            return 65.0
        else:
            return 50.0
    
    def _score_risk(self, symbol: str, data: Dict[str, Any]) -> float:
        """风险溢价（0-100）"""
        return 60.0


def get_scoring_engine_v76() -> ScoringEngineV76:
    """获取 v7.6 评分引擎实例"""
    return ScoringEngineV76()
