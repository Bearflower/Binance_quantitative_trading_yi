#!/usr/bin/env python3
"""
ATR（Average True Range）计算工具
用于动态止盈止损计算
"""

import logging
from decimal import Decimal
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class ATRCalculator:
    """ATR 计算器"""
    
    def __init__(self, period: int = 14):
        """
        初始化 ATR 计算器
        
        Args:
            period: ATR 周期，默认 14 日
        """
        self.period = period
        logger.info(f"ATR 计算器初始化完成，周期：{period}")
    
    def calculate_atr(self, klines: List[List[Any]], period: Optional[int] = None) -> Decimal:
        """
        计算 ATR 值
        
        Args:
            klines: K 线数据列表
                   每项格式：[开盘时间，开盘价，最高价，最低价，收盘价，成交量，...]
                   索引：      0       1      2      3      4      5
            period: ATR 周期，默认使用初始化的周期
        
        Returns:
            ATR 值（Decimal 类型）
        """
        if period is None:
            period = self.period
        
        if len(klines) < period:
            logger.warning(f"K 线数据不足，需要{period}条，实际{len(klines)}条")
            return Decimal('0')
        
        # 提取最近 period 条 K 线数据
        recent_klines = klines[-period:]
        
        true_ranges = []
        
        for i, kline in enumerate(recent_klines):
            high = Decimal(kline[2])  # 最高价
            low = Decimal(kline[3])   # 最低价
            prev_close = None
            
            # 如果不是第一条 K 线，获取前一根收盘价
            if i > 0:
                prev_close = Decimal(recent_klines[i-1][4])  # 前一根收盘价
            
            # 计算真实波幅（True Range）
            tr = self._calculate_true_range(high, low, prev_close)
            true_ranges.append(tr)
        
        # 计算 ATR（简单平均）
        atr = sum(true_ranges) / len(true_ranges)
        
        logger.info(f"ATR({period}) 计算完成：{atr}")
        return atr.quantize(Decimal('0.01'))
    
    def _calculate_true_range(self, high: Decimal, low: Decimal, 
                             prev_close: Optional[Decimal]) -> Decimal:
        """
        计算单根 K 线的真实波幅（True Range）
        
        TR = MAX(MAX(HIGH-LOW), ABS(HIGH-PREV_CLOSE), ABS(LOW-PREV_CLOSE))
        
        Args:
            high: 最高价
            low: 最低价
            prev_close: 前一根收盘价（如果是第一条 K 线则为 None）
        
        Returns:
            真实波幅
        """
        # 方法 1：当前最高价 - 当前最低价
        tr1 = high - low
        
        if prev_close is not None:
            # 方法 2：|当前最高价 - 前收盘价|
            tr2 = abs(high - prev_close)
            # 方法 3：|当前最低价 - 前收盘价|
            tr3 = abs(low - prev_close)
            
            # 取三者最大值
            true_range = max(tr1, tr2, tr3)
        else:
            # 第一条 K 线，没有前收盘价，只使用 tr1
            true_range = tr1
        
        return true_range
    
    def calculate_atr_multiple(self, klines: List[List[Any]], 
                               periods: List[int]) -> Dict[int, Decimal]:
        """
        计算多个周期的 ATR 值
        
        Args:
            klines: K 线数据列表
            periods: 周期列表，如 [7, 14, 21]
        
        Returns:
            {周期：ATR 值} 的字典
        """
        results = {}
        
        for period in periods:
            atr = self.calculate_atr(klines, period)
            results[period] = atr
            logger.info(f"ATR({period}) = {atr}")
        
        return results
    
    def get_atr_multiplier(self, atr: Decimal, multiplier: float) -> Decimal:
        """
        获取 ATR 的倍数（用于止盈止损计算）
        
        Args:
            atr: ATR 值
            multiplier: 倍数，如 2.0 表示 2 倍 ATR
        
        Returns:
            ATR 的倍数
        """
        result = atr * Decimal(str(multiplier))
        return result.quantize(Decimal('0.01'))
    
    def calculate_stop_loss_price(self, entry_price: Decimal, atr: Decimal,
                                  side: str, multiplier: float = 2.0) -> Decimal:
        """
        计算止损价格
        
        Args:
            entry_price: 入场价格
            atr: ATR 值
            side: 方向 ('LONG' 或 'SHORT')
            multiplier: ATR 倍数，默认 2 倍
        
        Returns:
            止损价格
        """
        atr_multiple = self.get_atr_multiplier(atr, multiplier)
        
        if side == 'LONG':
            # 多头止损 = 入场价 - ATR 倍数
            stop_loss = entry_price - atr_multiple
        else:  # SHORT
            # 空头止损 = 入场价 + ATR 倍数
            stop_loss = entry_price + atr_multiple
        
        # 价格精度处理（假设 2 位小数）
        return stop_loss.quantize(Decimal('0.01'))
    
    def calculate_take_profit_price(self, entry_price: Decimal, atr: Decimal,
                                    side: str, multiplier: float = 4.0) -> Decimal:
        """
        计算止盈价格
        
        Args:
            entry_price: 入场价格
            atr: ATR 值
            side: 方向 ('LONG' 或 'SHORT')
            multiplier: ATR 倍数，默认 4 倍
        
        Returns:
            止盈价格
        """
        atr_multiple = self.get_atr_multiplier(atr, multiplier)
        
        if side == 'LONG':
            # 多头止盈 = 入场价 + ATR 倍数
            take_profit = entry_price + atr_multiple
        else:  # SHORT
            # 空头止盈 = 入场价 - ATR 倍数
            take_profit = entry_price - atr_multiple
        
        return take_profit.quantize(Decimal('0.01'))
    
    def should_move_stop_loss(self, current_price: Decimal, entry_price: Decimal,
                              atr: Decimal, side: str, 
                              multiplier: float = 2.0) -> bool:
        """
        判断是否应该移动止损
        
        当价格向有利方向移动超过 ATR 倍数时，应该移动止损到成本价或盈利位置
        
        Args:
            current_price: 当前价格
            entry_price: 入场价格
            atr: ATR 值
            side: 方向 ('LONG' 或 'SHORT')
            multiplier: ATR 倍数，默认 2 倍
        
        Returns:
            True 表示应该移动止损，False 表示保持原止损
        """
        atr_multiple = self.get_atr_multiplier(atr, multiplier)
        
        if side == 'LONG':
            # 多头：当前价 - 入场价 >= ATR 倍数
            favorable_move = current_price - entry_price
        else:  # SHORT
            # 空头：入场价 - 当前价 >= ATR 倍数
            favorable_move = entry_price - current_price
        
        should_move = favorable_move >= atr_multiple
        
        if should_move:
            logger.info(f"价格有利移动 {favorable_move} >= {atr_multiple} (ATR*{multiplier})，应该移动止损")
        
        return should_move
    
    def calculate_trailing_stop_price(self, current_price: Decimal, atr: Decimal,
                                      side: str, multiplier: float = 3.0) -> Decimal:
        """
        计算跟踪止损价格
        
        跟踪止损会随着价格向有利方向移动而调整
        
        Args:
            current_price: 当前价格
            atr: ATR 值
            side: 方向 ('LONG' 或 'SHORT')
            multiplier: ATR 倍数，默认 3 倍
        
        Returns:
            跟踪止损价格
        """
        atr_multiple = self.get_atr_multiplier(atr, multiplier)
        
        if side == 'LONG':
            # 多头跟踪止损 = 当前价 - ATR 倍数
            trailing_stop = current_price - atr_multiple
        else:  # SHORT
            # 空头跟踪止损 = 当前价 + ATR 倍数
            trailing_stop = current_price + atr_multiple
        
        return trailing_stop.quantize(Decimal('0.01'))


def create_atr_calculator(period: int = 14) -> ATRCalculator:
    """
    创建 ATR 计算器实例
    
    Args:
        period: ATR 周期，默认 14
    
    Returns:
        ATRCalculator 实例
    """
    return ATRCalculator(period=period)


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("ATR 计算器测试")
    print("=" * 60)
    
    # 模拟 K 线数据
    # [开盘时间，开盘价，最高价，最低价，收盘价，成交量]
    test_klines = [
        [1, Decimal('50000'), Decimal('50500'), Decimal('49500'), Decimal('50200'), 1000],
        [2, Decimal('50200'), Decimal('50800'), Decimal('50000'), Decimal('50600'), 1200],
        [3, Decimal('50600'), Decimal('51000'), Decimal('50300'), Decimal('50800'), 1100],
        # ... 更多 K 线数据
    ]
    
    # 创建计算器
    atr_calc = create_atr_calculator(period=3)
    
    # 计算 ATR
    atr = atr_calc.calculate_atr(test_klines)
    print(f"ATR(3) = {atr}")
    
    # 计算止盈止损
    entry_price = Decimal('50000')
    side = 'LONG'
    
    stop_loss = atr_calc.calculate_stop_loss_price(entry_price, atr, side, multiplier=2.0)
    take_profit = atr_calc.calculate_take_profit_price(entry_price, atr, side, multiplier=4.0)
    
    print(f"入场价：{entry_price}")
    print(f"止损价（2 倍 ATR）: {stop_loss}")
    print(f"止盈价（4 倍 ATR）: {take_profit}")
    
    print("\nATR 计算器测试完成")
