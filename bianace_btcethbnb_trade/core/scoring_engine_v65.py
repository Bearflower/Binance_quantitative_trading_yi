#!/usr/bin/env python3
"""
评分引擎 v6.5 - v4 与 v6 融合版

核心改进（相比 v6）：
1. ADX 最小值：15 → 16（适度提高）
2. 成交量倍数（S 级）：1.5 → 1.6
3. 成交量倍数（A 级）：1.5 → 1.3
4. ATR% 上限：6% → 5.5%
5. ATR% 下限：1.5% → 1.5%（保持）

目标：
- 交易数：150-250 笔/6 个月
- 夏普比率：≥0.6
- 净利润：+120-150%

Author: Trading System
Version: 6.5.0
"""

import math
from typing import Dict, Any, Optional, List, Tuple
from collections import defaultdict, deque
import yaml
from pathlib import Path


class ScoringEngineV65:
    """评分引擎 v6.5 - v4 与 v6 融合版"""
    
    def __init__(self, config_file: str = 'config/scoring_params_v65.yaml'):
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
        """默认配置（v6.5 折中版）"""
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
                    'S': 75,
                    'A': 65,
                    'B': 55,
                    'C': 45
                },
                'market_filter': {
                    'min_adx': 14,           # v6.6: 16 → 14
                    'min_volume_ratio_s': 1.4, # v6.6: 1.6 → 1.4
                    'min_volume_ratio_a': 1.2, # v6.6: 1.3 → 1.2
                    'min_atr_pct': 0.01,     # v6.6: 1.5% → 1%
                    'max_atr_pct': 0.06      # v6.6: 5.5% → 6%
                }
            },
            'trading': {
                'leverage': 3,
                'max_positions': 3
            }
        }
    
    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """执行评分（v6.5 主入口）"""
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
        
        # 3. 市场状态过滤（v6.5 折中）
        market_state = self._check_market_state_v65(data)
        if market_state != 'trending':
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': f'市场状态={market_state}'
            }
        
        # 4. 6 维度评分
        indicators = data.get('indicators', {})
        
        trend_strength = self._score_trend_strength(indicators)
        trend_consistency = self._score_trend_consistency(indicators)
        pattern = self._score_pattern(indicators)
        volume = self._score_volume(indicators)
        momentum = self._score_momentum(indicators)
        risk = self._score_risk(symbol, data)
        
        # 加权总分
        total_score = (
            trend_strength +
            trend_consistency +
            pattern +
            volume +
            momentum +
            risk
        )
        
        # 5. 确定等级
        grade = self._determine_grade(total_score)
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
        
        # 7. 计算仓位（v6 连续评分 + 映射）
        position_ratio = self._calculate_position_v6(grade, total_score, data)
        
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
    
    def _check_veto_v6(self, data: Dict[str, Any]) -> Optional[str]:
        """一票否决检查（v6）"""
        # 资金费率检查
        funding_rate = data.get('funding_rate', 0.0001)
        if abs(funding_rate) > 0.0008:
            return f'资金费率={funding_rate:.4%}'
        
        # 24h 涨跌幅检查
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
    
    def _check_market_state_v65(self, data: Dict[str, Any]) -> str:
        """市场状态过滤（v6.5 折中版）"""
        indicators = data.get('indicators', {})
        filters = self.config['scoring']['market_filter']
        
        # ADX 检查（v6.5: ≥16）
        if '1d' in indicators:
            adx = indicators['1d'].get('adx', [0])[-1]
            if not isinstance(adx, (int, float)):
                adx = 0
            if adx < filters['min_adx']:
                return 'ranging'
        
        # 成交量检查（v6.5: S 级≥1.6, A 级≥1.3）
        if '1d' in indicators:
            volumes = indicators['1d'].get('volume', [])
            if len(volumes) >= 20:
                avg_vol = sum(volumes[-20:-1]) / 20
                current_vol = volumes[-1]
                if avg_vol > 0:
                    ratio = current_vol / avg_vol
                    # 使用 S 级标准 1.6
                    if ratio < filters['min_volume_ratio_s']:
                        return 'low_volume'
        
        # ATR 检查（v6.5: 1.5-5.5%）
        if '1d' in indicators:
            atr = indicators['1d'].get('atr14', [0])[-1]
            close = indicators['1d'].get('close', [1])[-1]
            if close > 0 and atr > 0:
                atr_pct = atr / close
                if atr_pct < filters['min_atr_pct']:
                    return 'too_low_volatility'
                if atr_pct > filters['max_atr_pct']:
                    return 'too_high_volatility'
        
        return 'trending'
    
    def _determine_grade(self, score: float) -> Optional[str]:
        """确定等级"""
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
    
    def _calculate_position_v6(self, grade: str, score: float, data: Dict[str, Any]) -> float:
        """v6 连续评分 + 仓位映射"""
        # 基础仓位系数
        base_position = 0.30 if grade == 'S' else 0.20
        
        # 分数映射（连续）
        score_factor = min(1.0, (score - 50) / 30)  # 50 分=0, 80 分=1
        
        # 波动率调整
        vol_adjustment = self._calculate_volatility_target(data)
        
        # 最终仓位
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
        
        # 各币种中位数 ATR%
        median_atr = {
            'BTCUSDT': 0.028,
            'ETHUSDT': 0.032,
            'BNBUSDT': 0.050
        }.get('BTCUSDT', 0.03)  # 简化，实际应传入 symbol
        
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
    
    def _score_pattern(self, indicators: Dict[str, Any]) -> float:
        """形态质量（0-30）"""
        return 20.0
    
    def _score_volume(self, indicators: Dict[str, Any]) -> float:
        """成交量（0-10）"""
        if '1d' not in indicators:
            return 5.0
        
        volumes = indicators['1d'].get('volume', [])
        if len(volumes) < 20:
            return 5.0
        
        avg_vol = sum(volumes[-20:-1]) / 20
        current_vol = volumes[-1]
        
        if avg_vol == 0:
            return 5.0
        
        ratio = current_vol / avg_vol
        
        if ratio >= 2.5:
            return 9.5
        elif ratio >= 2.0:
            return 8.5
        elif ratio >= 1.8:
            return 7.5
        elif ratio >= 1.6:
            return 6.5
        elif ratio >= 1.3:
            return 5.5
        else:
            return 4.5
    
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
    
    def _score_risk(self, symbol: str, data: Dict[str, Any]) -> float:
        """风险溢价（0-10）"""
        return 6.0


def get_scoring_engine_v65() -> ScoringEngineV65:
    """获取 v6.5 评分引擎实例"""
    return ScoringEngineV65()
