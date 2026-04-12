"""
K 线形态识别模块

负责识别：
- 长上影线
- 放量滞涨
- 三次冲顶失败
- 高点逐次下移
"""

from typing import List, Dict, Any, Optional, Tuple
from utils.logger import logger


class CandlestickPatternRecognizer:
    """K 线形态识别器"""
    
    def __init__(self):
        """初始化形态识别器"""
        # 形态识别参数
        self.long_shadow_ratio = 2.0  # 长上影线比例 (上影线/实体)
        self.volume_increase_ratio = 0.5  # 放量比例 (50%)
        self.price_stagnation_ratio = 0.02  # 滞涨比例 (2%)
        
        logger.info("✅ K 线形态识别器初始化完成")
    
    def detect_long_upper_shadow(
        self,
        kline: Dict[str, Any],
        ratio: float = None
    ) -> bool:
        """
        检测长上影线
        
        Args:
            kline: K 线数据字典
            ratio: 上影线/实体比例阈值 (默认 2.0)
            
        Returns:
            是否为长上影线
            
        判断标准:
            上影线长度 > 实体长度 × ratio
        """
        ratio = ratio or self.long_shadow_ratio
        
        open_price = kline['open']
        close_price = kline['close']
        high_price = kline['high']
        low_price = kline['low']
        
        # 计算实体长度
        body = abs(close_price - open_price)
        
        # 计算上影线长度
        upper_shadow = high_price - max(open_price, close_price)
        
        # 避免除零
        if body == 0:
            return False
        
        # 判断
        is_long_shadow = upper_shadow > (body * ratio)
        
        if is_long_shadow:
            logger.debug(
                f"📊 检测到长上影线：上影线={upper_shadow:.4f}, "
                f"实体={body:.4f}, 比例={upper_shadow/body:.2f}"
            )
        
        return is_long_shadow
    
    def detect_volume_stagnation(
        self,
        current_kline: Dict[str, Any],
        prev_kline: Dict[str, Any],
        volume_ratio: float = None,
        price_ratio: float = None
    ) -> bool:
        """
        检测放量滞涨
        
        Args:
            current_kline: 当前 K 线
            prev_kline: 前一根 K 线
            volume_ratio: 成交量放大比例 (默认 0.5 = 50%)
            price_ratio: 价格滞涨比例 (默认 0.02 = 2%)
            
        Returns:
            是否为放量滞涨
            
        判断标准:
            成交量放大 > 50% 且 价格涨幅 < 2%
        """
        volume_ratio = volume_ratio or self.volume_increase_ratio
        price_ratio = price_ratio or self.price_stagnation_ratio
        
        current_volume = current_kline['volume']
        prev_volume = prev_kline['volume']
        
        current_close = current_kline['close']
        prev_close = prev_kline['close']
        
        # 计算成交量变化
        volume_change = (current_volume - prev_volume) / prev_volume
        
        # 计算价格变化
        price_change = (current_close - prev_close) / prev_close
        
        # 判断
        is_stagnation = (
            volume_change > volume_ratio and
            abs(price_change) < price_ratio
        )
        
        if is_stagnation:
            logger.debug(
                f"📊 检测到放量滞涨：成交量变化={volume_change:.2%}, "
                f"价格变化={price_change:.2%}"
            )
        
        return is_stagnation
    
    def detect_triple_top(
        self,
        klines: List[Dict[str, Any]],
        tolerance: float = 0.02
    ) -> bool:
        """
        检测三次冲顶失败
        
        Args:
            klines: K 线数据列表 (至少 3 条)
            tolerance: 价格容差 (默认 2%)
            
        Returns:
            是否为三次冲顶
            
        判断标准:
            三次冲击同一高点，价格无法突破 (容差范围内)
        """
        if len(klines) < 3:
            return False
        
        # 获取最近 3 个高点
        highs = [kline['high'] for kline in klines[-3:]]
        
        # 找到最高价
        max_high = max(highs)
        
        # 检查是否三次都接近最高价
        count = 0
        for high in highs:
            if high >= max_high * (1 - tolerance):
                count += 1
        
        is_triple_top = (count == 3)
        
        if is_triple_top:
            logger.debug(
                f"📊 检测到三次冲顶：高点={highs}, "
                f"最高={max_high:.4f}"
            )
        
        return is_triple_top
    
    def detect_lower_highs(
        self,
        klines: List[Dict[str, Any]],
        count: int = 3
    ) -> bool:
        """
        检测高点逐次下移
        
        Args:
            klines: K 线数据列表
            count: 需要检测的高点数量 (默认 3)
            
        Returns:
            是否高点逐次下移
        """
        if len(klines) < count:
            return False
        
        # 获取最近 count 个高点
        highs = [kline['high'] for kline in klines[-count:]]
        
        # 检查是否逐次下降
        is_declining = all(
            highs[i] > highs[i + 1] for i in range(len(highs) - 1)
        )
        
        if is_declining:
            logger.debug(f"📊 检测到高点下移：高点={highs}")
        
        return is_declining
    
    def detect_patterns(
        self,
        klines: List[Dict[str, Any]]
    ) -> Dict[str, bool]:
        """
        综合检测所有形态
        
        Args:
            klines: K 线数据列表
            
        Returns:
            形态检测结果字典
        """
        if not klines or len(klines) < 2:
            logger.warning("⚠️ K 线数据不足，无法检测形态")
            return {
                'long_upper_shadow': False,
                'volume_stagnation': False,
                'triple_top': False,
                'lower_highs': False,
            }
        
        # 检测各种形态
        results = {
            'long_upper_shadow': self.detect_long_upper_shadow(klines[-1]),
            'volume_stagnation': self.detect_volume_stagnation(
                klines[-1], klines[-2]
            ),
            'triple_top': self.detect_triple_top(klines) if len(klines) >= 3 else False,
            'lower_highs': self.detect_lower_highs(klines),
        }
        
        # 统计检测到的形态数量
        pattern_count = sum(results.values())
        logger.debug(f"📊 检测到 {pattern_count}/4 个看跌形态")
        
        return results


# 全局识别器实例
pattern_recognizer = CandlestickPatternRecognizer()
