#!/usr/bin/env python3
"""
评分引擎 v8 - 夏普比率≥0.8 终极版

核心创新：
1. 动态信号排序阈值（根据 ADX 调整）
2. 策略权重自适应（趋势/震荡市自动切换）
3. 分段线性仓位映射
4. 波动率目标仓位管理
5. 连续评分优化

Author: Trading System
Version: 8.0.0
"""

import math
from typing import Dict, Any, Optional, List, Tuple
from collections import defaultdict, deque
import yaml
from pathlib import Path


class ScoringEngineV8:
    """v8 评分引擎 - 夏普比率优化版"""
    
    def __init__(self, config_file: str = 'config/scoring_params_v8.yaml'):
        self.config = self._load_config(config_file)
        
        # 信号历史（用于排序）
        self.signal_history = deque(maxlen=100)
        
        # 波动率历史（用于波动率目标）
        self.volatility_history = defaultdict(lambda: deque(maxlen=30))
        
        # 中位数 ATR%（用于波动率目标）
        self.median_atr_pct = {
            'BTCUSDT': 0.028,
            'ETHUSDT': 0.032,
            'BNBUSDT': 0.050
        }
        
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
                'min_score_to_trade': 50,
                'position_mapping': {
                    '80': 1.0,
                    '70': 0.8,
                    '60': 0.6,
                    '50': 0.4
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
            'frequency': {
                'max_per_day': 4,
                'cooldown_hours': 24,
                'pause_after_3_loss': True
            }
        }
    
    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        v8 主评分入口
        
        1. 三策略独立评分
        2. 根据 ADX 自适应权重
        3. 动态排序阈值
        4. 分段仓位映射
        5. 波动率目标调整
        """
        # 1. 获取当前市场状态（ADX）
        adx = self._get_current_adx(data)
        
        # 2. 三策略独立评分
        trend_score = self._score_trend_v8(symbol, data)
        reversal_score = self._score_reversal_v8(symbol, data)
        breakout_score = self._score_breakout_v8(symbol, data)
        
        # 3. 自适应权重（根据 ADX）
        weights = self._get_adaptive_weights(adx)
        
        # 4. 合并总分
        total_score = (
            trend_score['total'] * weights['trend'] +
            reversal_score['total'] * weights['reversal'] +
            breakout_score['total'] * weights['breakout']
        )
        
        # 5. 合并方向
        direction_score = (
            trend_score['direction_score'] * weights['trend'] +
            reversal_score['direction_score'] * weights['reversal'] +
            breakout_score['direction_score'] * weights['breakout']
        )
        
        # 6. 记录信号历史
        self.signal_history.append({
            'symbol': symbol,
            'score': total_score,
            'adx': adx,
            'time': data.get('timestamp', '')
        })
        
        # 7. 动态排序阈值（根据 ADX）
        top_percent = self._get_dynamic_threshold(adx)
        percentile = self._calculate_percentile(total_score)
        
        # 8. 检查是否在前 N%
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
        
        # 9. 检查最低分数（放宽至 45 分）
        if total_score < 45:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'percentile': percentile
            }
        
        # 10. 确定方向
        if abs(direction_score) < 0.3:
            direction = None
        elif direction_score > 0:
            direction = '多'
        else:
            direction = '空'
        
        if direction is None:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'percentile': percentile
            }
        
        # 11. 分段线性仓位映射
        position_coefficient = self._get_position_coefficient(total_score)
        
        # 12. 基础仓位系数
        base_coefficient = self.config['scoring']['base_position']['S'] if total_score >= 80 else self.config['scoring']['base_position']['A']
        
        # 13. 百分位微调（前 10% 额外 +10%）
        percentile_bonus = 1.1 if percentile > 0.90 else 1.0
        
        # 14. 波动率目标调整
        vol_adjustment = self._calculate_volatility_target(symbol, data)
        
        # 15. 最终仓位
        position_ratio = base_coefficient * position_coefficient * percentile_bonus * vol_adjustment
        position_ratio = min(0.50, max(0.0, position_ratio))
        
        # 16. 确定等级
        if total_score >= 80:
            grade = 'S'
        elif total_score >= 70:
            grade = 'A'
        elif total_score >= 60:
            grade = 'B'
        else:
            grade = 'C'
        
        return {
            'symbol': symbol,
            'score': total_score,
            'grade': grade,
            'direction': direction,
            'position_ratio': position_ratio,
            'percentile': percentile,
            'direction_score': direction_score,
            'adx': adx,
            'vol_adjustment': vol_adjustment,
            'breakdown': {
                'trend': trend_score,
                'reversal': reversal_score,
                'breakout': breakout_score
            }
        }
    
    def _get_current_adx(self, data: Dict[str, Any]) -> float:
        """获取当前 ADX"""
        indicators = data.get('indicators', {})
        if '1d' not in indicators:
            return 20.0
        
        adx = indicators['1d'].get('adx', [20])[-1]
        if not isinstance(adx, (int, float)):
            return 20.0
        
        return adx
    
    def _get_adaptive_weights(self, adx: float) -> Dict[str, float]:
        """
        根据 ADX 获取自适应权重
        
        ADX>30: 趋势 60%, 反转 10%, 突破 30%
        20<ADX≤30: 趋势 40%, 反转 20%, 突破 40%
        ADX≤20: 趋势 20%, 反转 50%, 突破 30%
        """
        if adx > 30:
            return {'trend': 0.60, 'reversal': 0.10, 'breakout': 0.30}
        elif adx > 20:
            return {'trend': 0.40, 'reversal': 0.20, 'breakout': 0.40}
        else:
            return {'trend': 0.20, 'reversal': 0.50, 'breakout': 0.30}
    
    def _get_dynamic_threshold(self, adx: float) -> float:
        """动态排序阈值（v8 放宽版）"""
        if adx > 30:
            return 0.40  # 放宽至 40%
        elif adx > 20:
            return 0.35  # 放宽至 35%
        else:
            return 0.25  # 放宽至 25%
    
    def _get_position_coefficient(self, score: float) -> float:
        """
        分段线性仓位映射
        
        ≥80: 1.0
        70-79: 0.8
        60-69: 0.6
        50-59: 0.4
        <50: 0.0
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
        else:
            return 0.0
    
    def _calculate_volatility_target(self, symbol: str, data: Dict[str, Any]) -> float:
        """
        波动率目标调整
        
        风险金额 = 10U × (中位数 ATR% / 当前 ATR%)
        限制在 [0.5, 1.5] 之间
        """
        indicators = data.get('indicators', {})
        if '1d' not in indicators:
            return 1.0
        
        atr = indicators['1d'].get('atr14', [0])[-1]
        close = indicators['1d'].get('close', [1])[-1]
        
        if close == 0 or atr == 0:
            return 1.0
        
        current_atr_pct = atr / close
        median_atr = self.median_atr_pct.get(symbol, 0.03)
        
        # 计算调整系数
        vol_adjustment = median_atr / current_atr_pct
        
        # 限制在 0.5-1.5 之间
        vol_adjustment = max(0.5, min(1.5, vol_adjustment))
        
        # 记录历史
        self.volatility_history[symbol].append(current_atr_pct)
        
        return vol_adjustment
    
    def _calculate_percentile(self, score: float) -> float:
        """计算百分位"""
        if len(self.signal_history) == 0:
            return 1.0
        
        scores = [s['score'] for s in self.signal_history]
        rank = sum(1 for s in scores if s < score)
        return rank / len(scores)
    
    # ========== 三策略评分函数 ==========
    
    def _score_trend_v8(self, symbol: str, data: Dict[str, Any]) -> Dict[str, float]:
        """趋势策略评分（v8）"""
        indicators = data.get('indicators', {})
        
        # 趋势强度
        trend_strength = self._score_trend_strength_v8(indicators)
        # 趋势一致性
        trend_consistency = self._score_trend_consistency_v8(indicators)
        # ADX
        adx_score = self._score_adx_v8(indicators)
        
        total = trend_strength * 0.40 + trend_consistency * 0.40 + adx_score * 0.20
        
        direction_score = 1.0 if trend_consistency > 60 else (-1.0 if trend_consistency < 40 else 0.0)
        position_coefficient = total / 100
        
        return {
            'total': total,
            'direction_score': direction_score,
            'position_coefficient': position_coefficient
        }
    
    def _score_reversal_v8(self, symbol: str, data: Dict[str, Any]) -> Dict[str, float]:
        """反转策略评分（v8）"""
        indicators = data.get('indicators', {})
        
        rsi_score = self._score_rsi_v8(indicators)
        macd_score = self._score_macd_v8(indicators)
        pattern_score = self._score_pattern_v8(indicators)
        
        total = rsi_score * 0.40 + macd_score * 0.40 + pattern_score * 0.20
        
        direction_score = 1.0 if rsi_score < 30 else (-1.0 if rsi_score > 70 else 0.0)
        position_coefficient = total / 100
        
        return {
            'total': total,
            'direction_score': direction_score,
            'position_coefficient': position_coefficient
        }
    
    def _score_breakout_v8(self, symbol: str, data: Dict[str, Any]) -> Dict[str, float]:
        """突破策略评分（v8）"""
        indicators = data.get('indicators', {})
        
        volume_score = self._score_volume_v8(indicators)
        breakout_score = self._score_breakout_morph_v8(indicators)
        vol_score = self._score_volatility_v8(symbol, indicators)
        
        total = volume_score * 0.40 + breakout_score * 0.40 + vol_score * 0.20
        
        direction_score = 1.0 if breakout_score > 60 else (-1.0 if breakout_score < 40 else 0.0)
        position_coefficient = total / 100
        
        return {
            'total': total,
            'direction_score': direction_score,
            'position_coefficient': position_coefficient
        }
    
    # ========== 连续评分函数（0-100）==========
    
    def _score_trend_strength_v8(self, indicators: Dict[str, Any]) -> float:
        """趋势强度（0-100）"""
        score = 50.0
        
        ema_count = 0
        for tf in ['1d', '4h', '1h']:
            if tf not in indicators:
                continue
            tf_data = indicators[tf]
            ema21 = tf_data.get('ema21', [])
            ema50 = tf_data.get('ema50', [])
            
            if not isinstance(ema21, list):
                ema21 = [ema21]
            if not isinstance(ema50, list):
                ema50 = [ema50]
            
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
    
    def _score_trend_consistency_v8(self, indicators: Dict[str, Any]) -> float:
        """趋势一致性（0-100）"""
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
        
        if len(directions) >= 2:
            if all(d == directions[0] for d in directions):
                return 90.0
            elif directions.count(1) == 2 or directions.count(-1) == 2:
                return 70.0
            else:
                return 40.0
        
        return 50.0
    
    def _score_adx_v8(self, indicators: Dict[str, Any]) -> float:
        """ADX 评分（0-100）"""
        if '1d' not in indicators:
            return 50.0
        
        adx = indicators['1d'].get('adx', [0])[-1]
        if not isinstance(adx, (int, float)):
            adx = 0
        
        if adx > 40:
            return 95.0
        elif adx > 30:
            return 85.0
        elif adx > 25:
            return 75.0
        elif adx > 20:
            return 65.0
        elif adx > 15:
            return 55.0
        else:
            return 45.0
    
    def _score_rsi_v8(self, indicators: Dict[str, Any]) -> float:
        """RSI 评分（0-100）"""
        if '1d' not in indicators:
            return 50.0
        
        rsi = indicators['1d'].get('rsi14', [])
        if not rsi:
            return 50.0
        
        rsi_val = rsi[-1]
        
        if rsi_val < 30:
            return 90.0
        elif rsi_val < 40:
            return 75.0
        elif rsi_val < 60:
            return 50.0
        elif rsi_val < 70:
            return 25.0
        else:
            return 10.0
    
    def _score_macd_v8(self, indicators: Dict[str, Any]) -> float:
        """MACD 评分（0-100）"""
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
    
    def _score_pattern_v8(self, indicators: Dict[str, Any]) -> float:
        """形态质量（0-100）"""
        return 65.0
    
    def _score_volume_v8(self, indicators: Dict[str, Any]) -> float:
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
    
    def _score_breakout_morph_v8(self, indicators: Dict[str, Any]) -> float:
        """突破形态（0-100）"""
        if '1d' not in indicators:
            return 50.0
        
        tf_data = indicators['1d']
        highs = tf_data.get('high', [])
        closes = tf_data.get('close', [])
        
        if len(highs) < 20 or len(closes) < 1:
            return 50.0
        
        highest_20 = max(highs[-20:-1])
        current_close = closes[-1]
        
        if current_close > highest_20:
            return 95.0
        elif current_close > highest_20 * 0.98:
            return 75.0
        else:
            return 50.0
    
    def _score_volatility_v8(self, symbol: str, indicators: Dict[str, Any]) -> float:
        """波动率评分（0-100）"""
        if '1d' not in indicators:
            return 50.0
        
        atr = indicators['1d'].get('atr14', [0])[-1]
        close = indicators['1d'].get('close', [1])[-1]
        
        if close == 0 or atr == 0:
            return 50.0
        
        current_atr_pct = atr / close
        best_atr = self.median_atr_pct.get(symbol, 0.03)
        
        deviation = abs(current_atr_pct - best_atr) / best_atr
        
        if deviation <= 0.3:
            score = 100.0 * (1 - deviation)
        else:
            score = max(0, 70.0 * (1 - deviation))
        
        return score


def get_scoring_engine_v8() -> ScoringEngineV8:
    """获取 v8 评分引擎实例"""
    return ScoringEngineV8()
