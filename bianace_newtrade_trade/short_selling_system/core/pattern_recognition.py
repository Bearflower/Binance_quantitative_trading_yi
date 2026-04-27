"""
形态识别模块

包含：
1. 三次冲顶检测
2. 长上影线检测
3. 放量滞涨检测
"""

from typing import List, Dict, Any, Tuple, Optional
from decimal import Decimal


class PatternRecognition:
    """形态识别器"""

    def __init__(self):
        """初始化形态识别器"""
        self.three_tops_threshold = Decimal('0.002')  # 0.2%
        self.long_shadow_ratio = Decimal('2.0')  # 上影线/实体 >= 2
        self.volume_ratio_threshold = Decimal('1.5')  # 成交量倍数
        self.min_klines = 5  # 最少K线数量

        print("✅ 形态识别器初始化完成")

    def detect_three_tops(
        self,
        klines: List[Dict[str, Any]],
        lookback: int = 5
    ) -> Tuple[bool, float, Optional[Decimal]]:
        """
        检测三次冲顶形态

        Args:
            klines: K线数据
            lookback: 回看K线数量

        Returns:
            (是否检测到, 得分, 阻力位) 元组
        """
        if len(klines) < self.min_klines:
            return False, 0.0, None

        recent_klines = klines[-lookback:]
        highs = [Decimal(str(k['high'])) for k in recent_klines]

        if len(highs) < 3:
            return False, 0.0, None

        max_high = max(highs)
        min_high = min(highs)

        if max_high == 0:
            return False, 0.0, None

        high_range_ratio = (max_high - min_high) / max_high

        if high_range_ratio < self.three_tops_threshold:
            resistance_level = max_high
            score = 4.0
            return True, score, resistance_level

        descending_count = 0
        for i in range(1, len(highs)):
            if highs[i] < highs[i-1]:
                descending_count += 1

        if descending_count >= 2:
            resistance_level = highs[0]
            score = 4.0
            return True, score, resistance_level

        same_level_count = 0
        for i in range(1, len(highs)):
            high_diff_ratio = abs(highs[i] - highs[i-1]) / highs[i-1]
            if high_diff_ratio < self.three_tops_threshold:
                same_level_count += 1

        if same_level_count >= 2:
            resistance_level = max_high
            score = 2.0
            return True, score, resistance_level

        return False, 0.0, None

    def detect_long_upper_shadow(
        self,
        kline: Dict[str, Any]
    ) -> Tuple[bool, float]:
        """
        检测长上影线

        Args:
            kline: 单根K线数据

        Returns:
            (是否检测到, 得分) 元组
        """
        high = Decimal(str(kline['high']))
        low = Decimal(str(kline['low']))
        open_price = Decimal(str(kline['open']))
        close = Decimal(str(kline['close']))

        upper_shadow = high - max(open_price, close)

        body = abs(close - open_price)

        is_doji = body / close < Decimal('0.001')

        if is_doji:
            if upper_shadow > close * Decimal('0.005'):
                return True, 3.0
            return False, 0.0

        if body == 0:
            return False, 0.0

        shadow_body_ratio = upper_shadow / body

        is_bearish = close < open_price

        if shadow_body_ratio >= self.long_shadow_ratio and (is_bearish or is_doji):
            return True, 3.0

        if shadow_body_ratio >= Decimal('1.5') and is_bearish:
            return True, 2.0

        return False, 0.0

    def detect_volume_divergence(
        self,
        klines: List[Dict[str, Any]]
    ) -> Tuple[bool, float, Optional[str]]:
        """
        检测放量滞涨

        Args:
            klines: K线数据

        Returns:
            (是否检测到, 得分, 原因) 元组
        """
        if len(klines) < 6:
            return False, 0.0, None

        current_kline = klines[-1]
        prev_klines = klines[-6:-1]

        current_volume = Decimal(str(current_kline['volume']))
        avg_volume = sum(Decimal(str(k['volume'])) for k in prev_klines) / Decimal(len(prev_klines))

        if avg_volume == 0:
            return False, 0.0, None

        volume_ratio = current_volume / avg_volume

        current_close = Decimal(str(current_kline['close']))
        prev_high = max(Decimal(str(k['high'])) for k in prev_klines)

        price_not_new_high = current_close < prev_high

        if volume_ratio >= self.volume_ratio_threshold and price_not_new_high:
            reason = f"放量滞涨：成交量倍数={volume_ratio:.2f}，价格未创新高"
            return True, 3.0, reason

        if volume_ratio >= Decimal('1.3') and price_not_new_high:
            reason = f"轻微放量滞涨：成交量倍数={volume_ratio:.2f}"
            return True, 2.0, reason

        return False, 0.0, None

    def analyze_patterns(
        self,
        klines: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        综合分析形态

        Args:
            klines: K线数据

        Returns:
            分析结果字典
        """
        if len(klines) < self.min_klines:
            return {
                'three_tops': {'detected': False, 'score': 0.0, 'resistance_level': None},
                'long_upper_shadow': {'detected': False, 'score': 0.0},
                'volume_divergence': {'detected': False, 'score': 0.0, 'reason': None},
                'total_score': 0.0,
                'data_insufficient': True
            }

        three_tops_detected, three_tops_score, resistance_level = self.detect_three_tops(klines)

        current_kline = klines[-1]
        long_upper_shadow, long_upper_shadow_score = self.detect_long_upper_shadow(current_kline)

        volume_divergence, volume_divergence_score, volume_reason = self.detect_volume_divergence(klines)

        total_score = three_tops_score + long_upper_shadow_score + volume_divergence_score

        return {
            'three_tops': {
                'detected': three_tops_detected,
                'score': three_tops_score,
                'resistance_level': float(resistance_level) if resistance_level else None
            },
            'long_upper_shadow': {
                'detected': long_upper_shadow,
                'score': long_upper_shadow_score
            },
            'volume_divergence': {
                'detected': volume_divergence,
                'score': volume_divergence_score,
                'reason': volume_reason
            },
            'total_score': total_score,
            'data_insufficient': False
        }


pattern_recognition = PatternRecognition()
