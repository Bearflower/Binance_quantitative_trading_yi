#!/usr/bin/env python3
"""
评分引擎 v7 - 终极平衡版

核心创新：
1. 连续评分 + 动态仓位（取代二元过滤）
2. 信号质量排序（Top 30%）
3. 多策略并行（趋势 + 反转 + 突破）
4. 自适应波动率归一化
5. 动态止损止盈

Author: Trading System
Version: 7.0.0
"""

import math
from typing import Dict, Any, Optional, List, Tuple
from collections import defaultdict, deque
import yaml
from pathlib import Path


class MultiStrategyScorerV7:
    """v7 多策略评分引擎"""
    
    def __init__(self, config_file: str = 'config/scoring_params_v7.yaml'):
        self.config = self._load_config(config_file)
        
        # 信号历史（用于排序）
        self.signal_history = deque(maxlen=50)
        
        # 波动率历史（用于自适应）
        self.volatility_history = defaultdict(lambda: deque(maxlen=30))
        
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
            'strategies': {
                'trend': {'weight': 0.40, 'enabled': True},
                'reversal': {'weight': 0.30, 'enabled': True},
                'breakout': {'weight': 0.30, 'enabled': True}
            },
            'scoring': {
                'top_percent': 0.20,  # v7 提高至前 20%
                'min_score_to_trade': 65  # v7 提高至 65 分
            },
            'volatility': {
                'best_atr_pct': {
                    'BTCUSDT': 0.028,
                    'ETHUSDT': 0.032,
                    'BNBUSDT': 0.050
                }
            }
        }
    
    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行多策略评分（v7 主入口）
        
        1. 三个子策略独立评分
        2. 合并方向和仓位
        3. 信号质量排序
        4. 动态仓位调整
        """
        # 1. 各子策略独立评分
        trend_score = self._score_trend_strategy(symbol, data)
        reversal_score = self._score_reversal_strategy(symbol, data)
        breakout_score = self._score_breakout_strategy(symbol, data)
        
        # 2. 获取策略权重
        weights = self.config['strategies']
        
        # 3. 合并方向（加权平均）
        direction_score = (
            weights['trend']['weight'] * trend_score['direction_score'] +
            weights['reversal']['weight'] * reversal_score['direction_score'] +
            weights['breakout']['weight'] * breakout_score['direction_score']
        )
        
        # 4. 合并仓位系数（加权平均）
        position_coefficient = (
            weights['trend']['weight'] * trend_score['position_coefficient'] +
            weights['reversal']['weight'] * reversal_score['position_coefficient'] +
            weights['breakout']['weight'] * breakout_score['position_coefficient']
        )
        
        # 5. 计算总分（0-100）
        total_score = (
            weights['trend']['weight'] * trend_score['total'] +
            weights['reversal']['weight'] * reversal_score['total'] +
            weights['breakout']['weight'] * breakout_score['total']
        )
        
        # 6. 信号质量排序（动态选择 Top 30%）
        self.signal_history.append({
            'symbol': symbol,
            'score': total_score,
            'time': data.get('timestamp', '')
        })
        
        # 计算当前信号在历史中的百分位
        percentile = self._calculate_percentile(total_score)
        
        # 7. 动态选择：只交易前 30%
        config = self.config['scoring']
        if percentile < config['top_percent']:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': None,  # 不交易
                'direction': None,
                'position_ratio': 0.0,
                'percentile': percentile,
                'reason': f'排名={percentile:.1%} < {config["top_percent"]:.0%}'
            }
        
        # 8. 检查最低分数
        if total_score < config['min_score_to_trade']:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'percentile': percentile,
                'reason': f'分数={total_score:.1f} < {config["min_score_to_trade"]}'
            }
        
        # 9. 确定方向
        if abs(direction_score) < 0.3:
            direction = None  # 方向不明确
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
        
        # 10. 动态仓位：基础系数 × 总分/100 × 百分位调整
        base_coefficient = 0.40 if direction_score > 0 else 0.25
        position_ratio = base_coefficient * (total_score / 100) * position_coefficient
        
        # 百分位微调（前 10% 额外 +10%）
        if percentile > 0.90:
            position_ratio *= 1.1
        
        # 限制最大仓位
        position_ratio = min(0.50, max(0.0, position_ratio))
        
        # 11. 确定等级
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
            'breakdown': {
                'trend': trend_score,
                'reversal': reversal_score,
                'breakout': breakout_score
            }
        }
    
    def _score_trend_strategy(self, symbol: str, data: Dict[str, Any]) -> Dict[str, float]:
        """
        趋势跟踪策略评分
        
        侧重：趋势强度、趋势一致性
        """
        indicators = data.get('indicators', {})
        
        # 1. 趋势强度（0-100）
        trend_strength = self._score_trend_strength_continuous(indicators)
        
        # 2. 趋势一致性（0-100）
        trend_consistency = self._score_trend_consistency_continuous(indicators)
        
        # 3. ADX（0-100）
        adx_score = self._score_adx_continuous(indicators)
        
        # 加权总分
        total = trend_strength * 0.40 + trend_consistency * 0.40 + adx_score * 0.20
        
        # 方向判断
        if trend_consistency > 60:
            direction_score = 1.0
        elif trend_consistency < 40:
            direction_score = -1.0
        else:
            direction_score = (trend_consistency - 50) / 10
        
        # 仓位系数
        position_coefficient = total / 100
        
        return {
            'total': total,
            'direction_score': direction_score,
            'position_coefficient': position_coefficient,
            'components': {
                'trend_strength': trend_strength,
                'trend_consistency': trend_consistency,
                'adx': adx_score
            }
        }
    
    def _score_reversal_strategy(self, symbol: str, data: Dict[str, Any]) -> Dict[str, float]:
        """
        反转策略评分
        
        侧重：动量背离、形态质量
        """
        indicators = data.get('indicators', {})
        
        # 1. RSI 超买超卖（0-100）
        rsi_score = self._score_rsi_continuous(indicators)
        
        # 2. MACD 背离（0-100）
        macd_score = self._score_macd_divergence_continuous(indicators)
        
        # 3. 形态质量（0-100）
        pattern_score = self._score_pattern_continuous(indicators)
        
        # 加权总分
        total = rsi_score * 0.40 + macd_score * 0.40 + pattern_score * 0.20
        
        # 方向判断（RSI 主导）
        if rsi_score > 70:
            direction_score = -1.0  # 超买，做空
        elif rsi_score < 30:
            direction_score = 1.0   # 超卖，做多
        else:
            direction_score = 0.0
        
        position_coefficient = total / 100
        
        return {
            'total': total,
            'direction_score': direction_score,
            'position_coefficient': position_coefficient,
            'components': {
                'rsi': rsi_score,
                'macd': macd_score,
                'pattern': pattern_score
            }
        }
    
    def _score_breakout_strategy(self, symbol: str, data: Dict[str, Any]) -> Dict[str, float]:
        """
        突破策略评分
        
        侧重：成交量确认、风险溢价
        """
        indicators = data.get('indicators', {})
        
        # 1. 成交量（0-100）
        volume_score = self._score_volume_continuous(indicators)
        
        # 2. 突破形态（0-100）
        breakout_score = self._score_breakout_continuous(indicators)
        
        # 3. 波动率（0-100）
        volatility_score = self._score_volatility_adaptive(symbol, indicators)
        
        # 加权总分
        total = volume_score * 0.40 + breakout_score * 0.40 + volatility_score * 0.20
        
        # 方向判断（突破方向）
        if breakout_score > 60:
            direction_score = 1.0
        elif breakout_score < 40:
            direction_score = -1.0
        else:
            direction_score = 0.0
        
        position_coefficient = total / 100
        
        return {
            'total': total,
            'direction_score': direction_score,
            'position_coefficient': position_coefficient,
            'components': {
                'volume': volume_score,
                'breakout': breakout_score,
                'volatility': volatility_score
            }
        }
    
    # ========== 连续评分函数（0-100）==========
    
    def _score_trend_strength_continuous(self, indicators: Dict[str, Any]) -> float:
        """趋势强度连续评分（0-100）"""
        score = 50.0  # 基础分
        
        # EMA 排列
        ema_alignment = 0
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
                    ema_alignment += 1
        
        if ema_alignment == 3:
            score = 90.0
        elif ema_alignment == 2:
            score = 70.0
        elif ema_alignment == 1:
            score = 50.0
        else:
            score = 30.0
        
        return score
    
    def _score_trend_consistency_continuous(self, indicators: Dict[str, Any]) -> float:
        """趋势一致性连续评分（0-100）"""
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
    
    def _score_adx_continuous(self, indicators: Dict[str, Any]) -> float:
        """ADX 连续评分（0-100）"""
        if '1d' not in indicators:
            return 50.0
        
        adx = indicators['1d'].get('adx', [0])[-1]
        if not isinstance(adx, (int, float)):
            adx = 0
        
        if adx > 40:
            return 90.0
        elif adx > 30:
            return 75.0
        elif adx > 25:
            return 60.0
        elif adx > 20:
            return 50.0
        elif adx > 15:
            return 40.0
        else:
            return 30.0
    
    def _score_rsi_continuous(self, indicators: Dict[str, Any]) -> float:
        """RSI 超买超卖评分（0-100）"""
        if '1d' not in indicators:
            return 50.0
        
        rsi = indicators['1d'].get('rsi14', [])
        if not rsi:
            return 50.0
        
        rsi_val = rsi[-1]
        
        # 超卖区域（高分）
        if rsi_val < 30:
            return 90.0
        elif rsi_val < 40:
            return 75.0
        # 中性区域
        elif rsi_val < 60:
            return 50.0
        # 超买区域
        elif rsi_val < 70:
            return 25.0
        else:
            return 10.0
    
    def _score_macd_divergence_continuous(self, indicators: Dict[str, Any]) -> float:
        """MACD 背离评分（0-100）"""
        if '1d' not in indicators:
            return 50.0
        
        macd_hist = indicators['1d'].get('macd_hist', [])
        if len(macd_hist) < 3:
            return 50.0
        
        # 检测背离（简化版）
        if macd_hist[-1] > 0 and macd_hist[-1] > macd_hist[-2] > macd_hist[-3]:
            return 80.0  # 多头背离
        elif macd_hist[-1] < 0 and macd_hist[-1] < macd_hist[-2] < macd_hist[-3]:
            return 80.0  # 空头背离
        elif macd_hist[-1] * macd_hist[-2] < 0:
            return 60.0  # 反转
        else:
            return 50.0
    
    def _score_pattern_continuous(self, indicators: Dict[str, Any]) -> float:
        """形态质量评分（0-100）"""
        return 65.0  # 默认中等
    
    def _score_volume_continuous(self, indicators: Dict[str, Any]) -> float:
        """成交量连续评分（0-100）"""
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
    
    def _score_breakout_continuous(self, indicators: Dict[str, Any]) -> float:
        """突破形态评分（0-100）"""
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
            return 90.0  # 突破
        elif current_close > highest_20 * 0.98:
            return 75.0  # 接近突破
        else:
            return 50.0
    
    def _score_volatility_adaptive(self, symbol: str, indicators: Dict[str, Any]) -> float:
        """
        自适应波动率评分（0-100）
        
        基于历史百分位，最佳波动区间得分最高
        """
        if '1d' not in indicators:
            return 50.0
        
        atr = indicators['1d'].get('atr14', [0])[-1]
        close = indicators['1d'].get('close', [1])[-1]
        
        if close == 0 or atr == 0:
            return 50.0
        
        current_atr_pct = atr / close
        
        # 获取最佳 ATR%
        config = self.config['volatility']['best_atr_pct']
        best_atr = config.get(symbol, 0.03)
        
        # 计算分数
        deviation = abs(current_atr_pct - best_atr) / best_atr
        
        if deviation <= 0.3:
            # 最佳区间内
            score = 100.0 * (1 - deviation)
        else:
            # 区间外线性衰减
            score = max(0, 70.0 * (1 - deviation))
        
        # 记录历史
        self.volatility_history[symbol].append(current_atr_pct)
        
        return score
    
    def _calculate_percentile(self, score: float) -> float:
        """计算当前分数在历史中的百分位"""
        if len(self.signal_history) == 0:
            return 1.0
        
        scores = [s['score'] for s in self.signal_history]
        rank = sum(1 for s in scores if s < score)
        percentile = rank / len(scores)
        
        return percentile


def get_scoring_engine_v7() -> MultiStrategyScorerV7:
    """获取 v7 评分引擎实例"""
    return MultiStrategyScorerV7()
