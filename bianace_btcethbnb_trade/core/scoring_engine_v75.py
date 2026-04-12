#!/usr/bin/env python3
"""
评分引擎 v7.5 - 夏普比率优化平衡版

核心改进（相比 v7 严格版）：
1. 信号排序百分比：Top 20% → Top 35%
2. 最低评分门槛：45 分 → 40 分
3. 增加 40-49 分段仓位系数 0.2
4. 保留所有 v7 有效机制（连续评分、吊灯止损等）

目标：
- 交易数：80-120 笔/6 个月
- 夏普比率：≥0.8
- 胜率：65-70%

Author: Trading System
Version: 7.5.0
"""

import math
from typing import Dict, Any, Optional, List
from collections import deque
import yaml
from pathlib import Path


class ScoringEngineV75:
    """v7.5 平衡版评分引擎"""
    
    def __init__(self, config_file: str = 'config/scoring_params_v75.yaml'):
        self.config = self._load_config(config_file)
        self.signal_history = deque(maxlen=200)  # 信号池扩大至 200
        
    def _load_config(self, config_file: str) -> Dict[str, Any]:
        """加载配置文件"""
        config_path = Path(__file__).parent.parent / config_file
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        return self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """默认配置"""
        return {
            'scoring': {
                'top_percent': 0.35,  # v7.5: Top 35%
                'min_score_to_trade': 40,  # v7.5: 最低 40 分
                'position_mapping': {
                    '80': 1.0,
                    '70': 0.8,
                    '60': 0.6,
                    '50': 0.4,
                    '40': 0.2  # v7.5 新增
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
                'min_adx': 20,
                'min_volume_ratio': 1.8,
                'max_atr_pct': 0.05
            }
        }
    
    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        v7.5 主评分入口
        
        1. 6 维度连续评分
        2. 信号排序（Top 35%）
        3. 分段仓位映射
        4. 波动率调整
        """
        # 1. 检查市场状态过滤
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
        
        # 6. 检查最低分数（v7.5: 40 分）
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
        
        # 8. 分段仓位映射（v7.5 新增 40-49 分段）
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
            grade = 'D'  # v7.5 新增
        
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
        """市场过滤（ADX、量比、ATR）"""
        indicators = data.get('indicators', {})
        filters = self.config['filters']
        
        # ADX 检查
        if '1d' in indicators:
            adx = indicators['1d'].get('adx', [0])[-1]
            if not isinstance(adx, (int, float)):
                adx = 0
            if adx < filters['min_adx']:
                return False
        
        # 量比检查
        if '1d' in indicators:
            volumes = indicators['1d'].get('volume', [])
            if len(volumes) >= 20:
                avg_vol = sum(volumes[-20:-1]) / 20
                current_vol = volumes[-1]
                if avg_vol > 0:
                    ratio = current_vol / avg_vol
                    if ratio < filters['min_volume_ratio']:
                        return False
        
        # ATR 检查
        if '1d' in indicators:
            atr = indicators['1d'].get('atr14', [0])[-1]
            close = indicators['1d'].get('close', [1])[-1]
            if close > 0 and atr > 0:
                atr_pct = atr / close
                if atr_pct > filters['max_atr_pct']:
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
        分段线性仓位映射（v7.5 新增 40-49 分段）
        
        ≥80: 1.0
        70-79: 0.8
        60-69: 0.6
        50-59: 0.4
        40-49: 0.2  # v7.5 新增
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


def get_scoring_engine_v75() -> ScoringEngineV75:
    """获取 v7.5 评分引擎实例"""
    return ScoringEngineV75()
