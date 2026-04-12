"""
形态评分系统
对符合形态的股票进行综合评分（0-100 分）
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Optional

from utils.logger import get_logger

logger = get_logger()


class PatternScorer:
    """形态评分器"""

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        """
        初始化评分器
        
        Args:
            weights: 各维度权重
        """
        self.weights = self._get_default_weights()
        if weights:
            self.weights.update(weights)

    def _get_default_weights(self) -> Dict[str, float]:
        """获取默认权重"""
        return {
            'drop_depth': 0.25,
            'shrink_degree': 0.20,
            'surge_strength': 0.20,
            'retrace_depth': 0.20,
            'retrace_shrink': 0.15
        }

    def score(self, detail: Dict[str, Any], params: Dict[str, Any]) -> float:
        """
        计算形态综合评分
        
        Args:
            detail: 形态检测详情
            params: 形态参数
        
        Returns:
            综合评分（0-100）
        """
        scores = {}
        
        scores['drop_depth'] = self._score_drop_depth(detail, params)
        scores['shrink_degree'] = self._score_shrink_degree(detail, params)
        scores['surge_strength'] = self._score_surge_strength(detail, params)
        scores['retrace_depth'] = self._score_retrace_depth(detail, params)
        scores['retrace_shrink'] = self._score_retrace_shrink(detail, params)
        
        total_score = 0
        for dim, score in scores.items():
            weight = self.weights.get(dim, 0)
            total_score += score * weight
        
        logger.debug(f"形态评分详情：{scores} => 综合：{total_score:.2f}")
        
        return round(total_score, 2)

    def _score_drop_depth(self, detail: Dict, params: Dict) -> float:
        """
        跌幅深度评分（20%=60 分，30%=100 分）
        """
        drop_rate = detail.get('drop_rate', 0)
        
        if drop_rate < 0.20:
            return 0
        
        if drop_rate >= 0.30:
            return 100
        
        score = 60 + (drop_rate - 0.20) / 0.10 * 40
        return min(100, max(0, score))

    def _score_shrink_degree(self, detail: Dict, params: Dict) -> float:
        """
        缩量程度评分（30%=60 分，50%=100 分）
        """
        min_vol_ratio = detail.get('min_vol_ratio', 1)
        
        if min_vol_ratio >= 0.50:
            return 60
        
        if min_vol_ratio <= 0.30:
            return 100
        
        score = 100 - (min_vol_ratio - 0.30) / 0.20 * 40
        return min(100, max(0, score))

    def _score_surge_strength(self, detail: Dict, params: Dict) -> float:
        """
        放量强度评分（1.5 倍=60 分，3 倍=100 分）
        """
        surge_volume_ratio = detail.get('surge_volume_ratio', 0)
        
        if surge_volume_ratio < 1.5:
            return 0
        
        if surge_volume_ratio >= 3.0:
            return 100
        
        score = 60 + (surge_volume_ratio - 1.5) / 1.5 * 40
        return min(100, max(0, score))

    def _score_retrace_depth(self, detail: Dict, params: Dict) -> float:
        """
        回踩幅度评分（0%=100 分，50%=60 分）
        """
        surge_price = detail.get('surge_price', 0)
        surge_open = detail.get('surge_open', 0)
        low_after_surge = detail.get('low_after_surge', surge_price)
        
        if surge_price == surge_open:
            candle_body = abs(surge_price - detail.get('surge_high', surge_price))
        else:
            candle_body = abs(surge_price - surge_open)
        
        if candle_body == 0:
            return 100
        
        retrace_depth = (surge_price - low_after_surge) / candle_body
        
        if retrace_depth <= 0:
            return 100
        
        if retrace_depth >= 0.50:
            return 60
        
        score = 100 - retrace_depth / 0.50 * 40
        return min(100, max(0, score))

    def _score_retrace_shrink(self, detail: Dict, params: Dict) -> float:
        """
        回踩缩量评分（<0.3=100 分，>0.7=60 分）
        """
        retrace_vol_ratio = detail.get('retrace_vol_ratio', 1)
        
        if retrace_vol_ratio <= 0.3:
            return 100
        
        if retrace_vol_ratio >= 0.7:
            return 60
        
        score = 100 - (retrace_vol_ratio - 0.3) / 0.4 * 40
        return min(100, max(0, score))

    def score_batch(self, details: list, params: Dict) -> list:
        """
        批量评分
        
        Args:
            details: 形态检测详情列表
            params: 形态参数
        
        Returns:
            评分列表
        """
        scores = []
        for detail in details:
            score = self.score(detail, params)
            detail['score'] = score
            scores.append(detail)
        
        scores.sort(key=lambda x: x.get('score', 0), reverse=True)
        return scores
