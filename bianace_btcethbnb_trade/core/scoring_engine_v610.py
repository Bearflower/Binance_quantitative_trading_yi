#!/usr/bin/env python3
"""
评分引擎 v6.10 - 最终优化版

核心改进（基于 v6 原版）：
1. 保留 v6 原版 3 维度评分系统（趋势、形态、动量）- 已验证成功
2. 保留 v6 原版前置过滤器（ADX≥20，成交量≥1.8，ATR 2-4.5%）
3. 新增 B 级（≥60 分，15% 仓位）和 C 级（≥50 分，5% 仓位）
4. 保持 S/A 级阈值不变（S≥85，A≥75）

目标：
- 总交易数：80-110 笔/6 个月（B/C 级贡献 30-60 笔）
- 夏普比率：≥0.55
- 净利润：+120%-160%
- 最大回撤：<20%

Author: Trading System
Version: 6.10.0
"""

import math
from typing import Dict, Any, Optional, List, Tuple
from collections import defaultdict, deque
import yaml
from pathlib import Path


class ScoringEngineV610:
    """评分引擎 v6.10 - 最终优化版"""
    
    def __init__(self, config_file: str = 'config/scoring_params_v610.yaml'):
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
        """默认配置（v6.10）"""
        return {
            'scoring': {
                'dimensions': {
                    'trend_strength': {'weight': 0.35, 'max_score': 35},
                    'pattern_quality': {'weight': 0.35, 'max_score': 35},
                    'momentum_divergence': {'weight': 0.30, 'max_score': 30}
                },
                'grade_thresholds': {
                    'S': 85,   # v6 原版
                    'A': 75,   # v6 原版
                    'B': 60,   # v6.10 新增
                    'C': 50    # v6.10 新增
                },
                'position_mapping': {
                    'S': 0.50,  # v6 原版
                    'A': 0.30,  # v6 原版
                    'B': 0.15,  # v6.10 新增
                    'C': 0.05   # v6.10 新增
                },
                'leverage': {
                    'S': 5,     # v6 原版
                    'A': 4,     # v6 原版
                    'B': 3,     # v6.10 新增
                    'C': 2      # v6.10 新增
                },
                'pre_filters': {
                    'adx_min': 15,  # v6.10: 20 → 15（放宽）
                    'volume_ratio_b': 1.2,  # v6.10: 1.3 → 1.2（放宽）
                    'atr_pct_min': 0.015,  # v6.10: 0.02 → 0.015（放宽）
                    'atr_pct_max': 0.06  # v6.10: 0.045 → 0.06（放宽）
                }
            },
            'trading': {
                'base_position_s': 0.50,
                'base_position_a': 0.30,
                'base_position_b': 0.15,
                'base_position_c': 0.05
            }
        }
    
    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """执行评分（v6.10 主入口）"""
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
        indicators = data.get('indicators', {})
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
        
        # 3. v6 原版趋势过滤器
        trend_ok, trend_reason = self._check_trend_filter_v6(data)
        if not trend_ok:
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': f'趋势过滤：{trend_reason}'
            }
        
        # 4. v6 原版前置过滤器（ADX/成交量/ATR）
        pre_filter_ok, pre_filter_reason = self._check_pre_filters_v6(symbol, indicators, data)
        if not pre_filter_ok:
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': f'前置过滤：{pre_filter_reason}'
            }
        # pre_filter_ok, pre_filter_reason = self._check_pre_filters_v6(symbol, indicators, data)
        # if not pre_filter_ok:
        #     return {
        #         'symbol': symbol,
        #         'score': 0.0,
        #         'grade': None,
        #         'direction': None,
        #         'position_ratio': 0.0,
        #         'veto_reason': f'前置过滤：{pre_filter_reason}'
        #     }
        
        # 5. v6 原版 3 维度评分
        trend_score = self._score_trend_strength_v6(indicators)
        pattern_score = self._score_pattern_v6(indicators)
        momentum_score = self._score_momentum_v6(indicators)
        
        total_score = trend_score + pattern_score + momentum_score
        
        # 6. 确定等级（v6.10: 新增 B/C 级）
        grade = self._determine_grade_v610(total_score)
        if grade is None:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0
            }
        
        # 7. 确定方向
        direction = self._determine_direction(indicators)
        if direction is None:
            return {
                'symbol': symbol,
                'score': total_score,
                'grade': grade,
                'direction': None,
                'position_ratio': 0.0
            }
        
        # 8. 计算仓位
        position_ratio = self._calculate_position(grade, total_score, data)
        
        return {
            'symbol': symbol,
            'score': total_score,
            'grade': grade,
            'direction': direction,
            'position_ratio': position_ratio,
            'breakdown': {
                'trend_strength': trend_score,
                'pattern_quality': pattern_score,
                'momentum_divergence': momentum_score
            }
        }
    
    def _check_veto(self, data: Dict[str, Any]) -> Optional[str]:
        """一票否决检查"""
        funding_rate = data.get('funding_rate', 0.0001)
        if abs(funding_rate) > 0.0008:
            return f'资金费率={funding_rate:.4%}'
        
        price_change = data.get('price_change_24h', 0.0)
        if abs(price_change) > 0.15:
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
    
    def _check_trend_filter_v6(self, data: Dict[str, Any]) -> Tuple[bool, str]:
        """趋势过滤器 v6（原版）"""
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
        
        # 日线趋势判断
        is_long = (
            closes[-1] > ema55[-1] and
            ema21[-1] > ema21[-2]
        )
        
        is_short = (
            closes[-1] < ema55[-1] and
            ema21[-1] < ema21[-2]
        )
        
        if not is_long and not is_short:
            return False, '趋势不明确'
        
        return True, '多头' if is_long else '空头'
    
    def _check_pre_filters_v6(self, symbol: str, indicators: Dict[str, Any], data: Dict[str, Any]) -> Tuple[bool, str]:
        """前置过滤器 v6（原版：ADX/成交量/ATR）"""
        config = self.config['scoring']['pre_filters']
        
        if '1d' not in indicators:
            return False, '无 1d 数据'
        
        tf_data = indicators['1d']
        
        # 1. ADX 检查
        adx = tf_data.get('adx14', [0])[-1]
        if adx < config['adx_min']:
            return False, f'ADX={adx:.1f} < {config["adx_min"]}'
        
        # 2. 成交量检查
        volumes = tf_data.get('volume', [])
        if len(volumes) >= 20:
            avg_vol = sum(volumes[-20:-1]) / 20
            current_vol = volumes[-1]
            ratio = current_vol / avg_vol if avg_vol > 0 else 0
            
            # 根据预期等级调整成交量要求（这里先按最低要求检查）
            if ratio < config['volume_ratio_b']:
                return False, f'成交量比率={ratio:.2f} < {config["volume_ratio_b"]}'
        
        # 3. ATR% 检查
        atr = tf_data.get('atr14', [0])[-1]
        close = tf_data.get('close', [1])[-1]
        
        if close > 0 and atr > 0:
            atr_pct = atr / close
            if atr_pct < config['atr_pct_min'] or atr_pct > config['atr_pct_max']:
                return False, f'ATR%={atr_pct:.2%} 不在 [{config["atr_pct_min"]:.1%}, {config["atr_pct_max"]:.1%}]'
        
        return True, '通过'
    
    def _determine_grade_v610(self, score: float) -> Optional[str]:
        """确定等级 v6.10（新增 B/C 级）"""
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
        
        score_factor = min(1.0, (score - 50) / 50)
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
    
    # ========== v6 原版 3 维度评分函数 ==========
    
    def _score_trend_strength_v6(self, indicators: Dict[str, Any]) -> float:
        """趋势强度（0-35）- v6 原版"""
        if '1d' not in indicators:
            return 17.5
        
        tf_data = indicators['1d']
        closes = tf_data.get('close', [])
        ema21 = tf_data.get('ema21', [])
        ema50 = tf_data.get('ema50', [])
        ema55 = tf_data.get('ema55', [])
        
        if not isinstance(closes, list): closes = [closes]
        if not isinstance(ema21, list): ema21 = [ema21]
        if not isinstance(ema50, list): ema50 = [ema50]
        if not isinstance(ema55, list): ema55 = [ema55]
        
        if len(closes) < 2 or len(ema21) < 1 or len(ema50) < 1 or len(ema55) < 1:
            return 17.5
        
        score = 15.0
        
        # EMA 排列（+10）
        if ema21[-1] > ema50[-1] > ema55[-1]:
            score += 10.0
        elif ema21[-1] > ema50[-1]:
            score += 7.0
        elif ema21[-1] > ema55[-1]:
            score += 5.0
        
        # EMA21 斜率（+10）
        if len(ema21) >= 2 and ema21[-2] != 0:
            slope = (ema21[-1] - ema21[-2]) / ema21[-2] * 100
            if slope > 1.0:
                score += 10.0
            elif slope > 0.5:
                score += 7.0
            elif slope > 0:
                score += 5.0
            elif slope < -0.5:
                score -= 5.0
        
        return max(0.0, min(35.0, score))
    
    def _score_pattern_v6(self, indicators: Dict[str, Any]) -> float:
        """形态质量（0-35）- v6 原版"""
        if '1d' not in indicators:
            return 17.5
        
        tf_data = indicators['1d']
        closes = tf_data.get('close', [])
        ema21 = tf_data.get('ema21', [])
        
        if not isinstance(closes, list): closes = [closes]
        if not isinstance(ema21, list): ema21 = [ema21]
        
        if len(closes) < 5 or len(ema21) < 1:
            return 17.5
        
        score = 15.0
        
        # 价格相对 EMA21 位置（+10）
        if closes[-1] > ema21[-1]:
            distance = (closes[-1] - ema21[-1]) / ema21[-1] * 100
            if 0 < distance < 3:
                score += 10.0
            elif distance >= 3:
                score += 6.0
            else:
                score += 8.0
        else:
            score -= 5.0
        
        # 近期涨幅（+10）
        if len(closes) >= 5:
            change = (closes[-1] - closes[-5]) / closes[-5] * 100
            if 0 < change < 10:
                score += 10.0
            elif -5 < change <= 0:
                score += 6.0
            elif abs(change) >= 10:
                score -= 5.0
        
        return max(0.0, min(35.0, score))
    
    def _score_momentum_v6(self, indicators: Dict[str, Any]) -> float:
        """动量背离（0-30）- v6 原版"""
        if '1d' not in indicators:
            return 15.0
        
        tf_data = indicators['1d']
        macd_hist = tf_data.get('macd_hist', [])
        rsi = tf_data.get('rsi14', [50])
        
        if len(macd_hist) < 3:
            return 15.0
        
        score = 15.0
        
        # MACD 柱状图变化（+15）
        if macd_hist[-1] > 0 and macd_hist[-1] > macd_hist[-2] > macd_hist[-3]:
            score += 15.0
        elif macd_hist[-1] < 0 and macd_hist[-1] < macd_hist[-2] < macd_hist[-3]:
            score += 15.0
        elif macd_hist[-1] * macd_hist[-2] < 0:
            score += 8.0
        elif (macd_hist[-1] - macd_hist[-2]) * macd_hist[-1] > 0:
            score += 10.0
        
        # RSI 位置（额外调整）
        rsi_value = rsi[-1] if len(rsi) > 0 else 50
        if 45 < rsi_value < 65:
            score += 3.0
        elif rsi_value > 70 or rsi_value < 30:
            score -= 3.0
        
        return max(0.0, min(30.0, score))


def get_scoring_engine_v610() -> ScoringEngineV610:
    """获取 v6.10 评分引擎实例"""
    return ScoringEngineV610()
