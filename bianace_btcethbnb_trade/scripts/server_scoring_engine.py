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
        
        1. ADX ≥18（放宽）- 如果数据缺失则跳过
        2. 量比≥1.8（放宽）- 如果数据缺失则跳过
        3. ATR 1.8%-5%（放宽）
        """
        indicators = data.get('indicators', {})
        filter_config = self.config['scoring']['market_filter']
        
        # 1. ADX 检查（放宽至 18）- 如果数据缺失则跳过
        if '1d' in indicators and 'adx' in indicators['1d']:
            adx = indicators['1d'].get('adx', [0])[-1]
            if not isinstance(adx, (int, float)):
                adx = 0
            if adx > 0 and adx < filter_config['min_adx']:
                return f'ADX={adx:.1f} < {filter_config["min_adx"]}'
        
        # 2. 成交量检查（放宽至 1.8）- 如果数据缺失则跳过
        if '1d' in indicators and 'volume' in indicators['1d']:
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
            atr14_data = indicators['1d'].get('atr14', 0)
            close_data = indicators['1d'].get('close', 1)
            # atr14 和 close 可能是 Decimal 对象或列表
            atr = float(atr14_data[-1]) if isinstance(atr14_data, list) else float(atr14_data)
            close = float(close_data[-1]) if isinstance(close_data, list) else float(close_data)
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
        仓位计算（v6.12 优化版 - 提高质量门槛）
        
        S 级：50%（重仓）
        A 级：35%（中仓）
        B 级：18%（轻仓）
        C 级：6%（极轻仓）
        """
        if grade is None:
            return 0.0
        
        if grade == 'S':
            return min(0.50, 0.45 + (score - 85) / 300)
        elif grade == 'A':
            return min(0.35, 0.30 + (score - 75) / 300)
        elif grade == 'B':
            return min(0.18, 0.15 + (score - 65) / 300)
        elif grade == 'C':
            return min(0.06, 0.05 + (score - 55) / 300)
        else:
            return 0.0


def get_scoring_engine_v6() -> ScoringEngineV6:
    """获取 v6 评分引擎实例"""
    return ScoringEngineV6()


class ScoringEngineV611(ScoringEngineV6):
    """评分引擎 v6.11 - 基于 v6 原版优化（新增 B/C 级，放宽频率）"""
    pass


def get_scoring_engine_v611() -> ScoringEngineV611:
    """获取 v6.11 评分引擎实例"""
    return ScoringEngineV611()


class ScoringEngineV612(ScoringEngineV6):
    """评分引擎 v6.12 - 方案 A 稳健型（提高 B/C 级门槛，减少每日交易）"""
    
    def _score_trend_strength_v6(self, indicators: Dict[str, Any]) -> float:
        """
        趋势强度（15 分）- 基于 EMA 斜率
        """
        if '1d' not in indicators:
            return 0.0
        
        ema21_data = indicators['1d'].get('ema21', [])
        # ema21 可能是 Decimal 对象或列表
        if isinstance(ema21_data, list):
            ema21 = [float(e) for e in ema21_data]
        else:
            # 如果是单个值，无法计算斜率，返回 0
            return 0.0
        
        if len(ema21) < 5:
            return 0.0
        
        # 计算 EMA 斜率（最近 5 日）
        recent_ema = ema21[-5:]
        ema_change = (recent_ema[-1] - recent_ema[0]) / recent_ema[0] if recent_ema[0] > 0 else 0
        
        # 斜率评分
        if ema_change > 0.05:  # >5%
            return 15.0
        elif ema_change > 0.03:  # >3%
            return 12.0
        elif ema_change > 0.01:  # >1%
            return 9.0
        elif ema_change > 0:
            return 6.0
        else:
            return 3.0
    
    def _score_trend_consistency_v6(self, indicators: Dict[str, Any]) -> float:
        """
        趋势一致性（15 分）- 多周期 EMA 方向一致性
        """
        directions = []
        for tf in ['1d', '4h', '1h']:
            if tf not in indicators:
                continue
            ema21 = indicators[tf].get('ema21_list', [])
            if len(ema21) >= 2:
                if ema21[-1] > ema21[-2]:
                    directions.append(1)
                else:
                    directions.append(-1)
        
        if not directions:
            return 0.0
        
        # 一致性评分
        if len(directions) == 3 and all(d == 1 for d in directions):
            return 15.0
        elif len(directions) >= 2 and sum(directions) > 0:
            return 12.0
        elif len(directions) >= 1 and sum(directions) > 0:
            return 9.0
        else:
            return 3.0
    
    def _score_pattern_v6(self, indicators: Dict[str, Any]) -> float:
        """
        形态质量（30 分）- 基于 RSI 和布林带位置
        """
        score = 0.0
        
        # RSI 评分（15 分）
        if '1d' in indicators:
            rsi = indicators['1d'].get('rsi', 50)
            if 40 <= rsi <= 60:  # 健康区间
                score += 15.0
            elif 30 <= rsi < 40 or 60 < rsi <= 70:  # 温和区间
                score += 10.0
            elif rsi < 30 or rsi > 70:  # 极端区间
                score += 5.0
        
        # 布林带位置评分（15 分）
        if '1d' in indicators:
            bollinger = indicators['1d'].get('bollinger', {})
            if isinstance(bollinger, dict):
                close_data = indicators['1d'].get('close', 0)
                close = float(close_data[-1]) if isinstance(close_data, list) else float(close_data)
                lower_data = bollinger.get('lower', [])
                upper_data = bollinger.get('upper', [])
                if lower_data and upper_data:
                    lower = float(lower_data[-1])
                    upper = float(upper_data[-1])
                    if lower > 0 and upper > lower:
                        position = (close - lower) / (upper - lower)
                        if 0.3 <= position <= 0.7:  # 中轨附近
                            score += 15.0
                        elif 0.1 <= position < 0.3 or 0.7 < position <= 0.9:  # 突破边缘
                            score += 10.0
                        else:  # 极端位置
                            score += 5.0
        
        return min(score, 30.0)
    
    def _score_volume_v6(self, indicators: Dict[str, Any]) -> float:
        """
        成交量（10 分）- 基于量比
        """
        if '1d' not in indicators:
            return 0.0
        
        volumes = indicators['1d'].get('volume', [])
        if len(volumes) < 20:
            return 0.0
        
        avg_vol = sum(volumes[-20:-1]) / 20 if sum(volumes[-20:-1]) > 0 else 1
        current_vol = volumes[-1]
        volume_ratio = current_vol / avg_vol if avg_vol > 0 else 0
        
        if volume_ratio > 2.0:
            return 10.0
        elif volume_ratio > 1.5:
            return 8.0
        elif volume_ratio > 1.2:
            return 6.0
        elif volume_ratio > 1.0:
            return 4.0
        else:
            return 2.0
    
    def _score_momentum_v6(self, indicators: Dict[str, Any]) -> float:
        """
        动量（20 分）- 基于 MACD 和价格动量
        """
        score = 0.0
        
        # MACD 评分（10 分）
        if '1d' in indicators:
            macd_data = indicators['1d'].get('macd', {})
            if isinstance(macd_data, dict):
                macd = macd_data.get('macd', 0)
                signal = macd_data.get('signal', 0)
                if macd > signal:  # 金叉
                    score += 10.0
                elif abs(macd - signal) < 0.01:  # 接近
                    score += 5.0
        
        # 价格动量评分（10 分）
        if '1d' in indicators:
            close_list = indicators['1d'].get('close_list', [])
            if len(close_list) >= 5:
                momentum_5d = (close_list[-1] - close_list[-5]) / close_list[-5] if close_list[-5] > 0 else 0
                if momentum_5d > 0.05:
                    score += 10.0
                elif momentum_5d > 0.02:
                    score += 7.0
                elif momentum_5d > 0:
                    score += 4.0
        
        return min(score, 20.0)
    
    def _score_risk_v6(self, symbol: str, data: Dict[str, Any]) -> float:
        """
        风险溢价（10 分）- 基于波动率和资金费率
        """
        score = 10.0
        
        # 波动率风险（5 分）
        if '1d' in data.get('indicators', {}):
            atr = data['indicators']['1d'].get('atr14', 0)
            close = data['indicators']['1d'].get('close', 1)
            if close > 0:
                volatility = atr / close
                if volatility > 0.05:  # >5%
                    score -= 5.0
                elif volatility > 0.03:  # >3%
                    score -= 2.5
        
        # 资金费率风险（5 分）
        funding_rate = abs(data.get('funding_rate', 0))
        if funding_rate > 0.001:  # >0.1%
            score -= 5.0
        elif funding_rate > 0.0005:  # >0.05%
            score -= 2.5
        
        return max(score, 0.0)
    
    def calculate_position_ratio(self, score: float, grade: str) -> float:
        """
        计算仓位比例（基于评分和等级）
        
        Args:
            score: 评分（0-100）
            grade: 信号等级（S/A/B/C）
        
        Returns:
            仓位比例（0.0-1.0）
        """
        # 基于等级的基础仓位比例
        grade_ratios = {
            'S': 0.20,  # S 级：20%
            'A': 0.15,  # A 级：15%
            'B': 0.10,  # B 级：10%
            'C': 0.05   # C 级：5%
        }
        
        base_ratio = grade_ratios.get(grade, 0.05)
        
        # 基于评分的调整系数（评分越高，仓位比例越高）
        score_factor = score / 100.0
        
        # 最终仓位比例 = 基础比例 × 评分系数
        position_ratio = base_ratio * score_factor
        
        return min(position_ratio, 0.20)  # 不超过 20%


def get_scoring_engine_v612() -> ScoringEngineV612:
    """获取 v6.12 评分引擎实例"""
    return ScoringEngineV612()


# v6.12 作为当前生产环境的默认版本
def get_scoring_engine() -> ScoringEngineV612:
    """获取当前生产环境评分引擎（v6.12）"""
    return ScoringEngineV612()
