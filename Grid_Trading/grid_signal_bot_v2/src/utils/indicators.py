"""
技术指标计算工具
用于计算 ADX、EMA、ATR 等技术指标
"""

from typing import List, Dict, Optional
from dataclasses import dataclass


@dataclass
class KlineData:
    """K线数据结构"""
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float


class TechnicalIndicators:
    """技术指标计算器"""
    
    @staticmethod
    def calculate_adx(
        klines: List[Dict], 
        period: int = 14
    ) -> Optional[float]:
        """
        计算平均方向指数 (ADX)
        
        Args:
            klines: K线数据列表
            period: 周期
            
        Returns:
            ADX 值
        """
        if len(klines) < period * 2:
            return None
        
        # 计算 +DM 和 -DM
        plus_dm = []
        minus_dm = []
        true_ranges = []
        
        for i in range(1, len(klines)):
            current = klines[i]
            previous = klines[i - 1]
            
            # 获取价格
            current_high = current.get('high_price', 0)
            current_low = current.get('low_price', 0)
            previous_close = previous.get('close_price', 0)
            
            # 计算 True Range
            high_low = current_high - current_low
            high_close = abs(current_high - previous_close)
            low_close = abs(current_low - previous_close)
            tr = max(high_low, high_close, low_close)
            true_ranges.append(tr)
            
            # 计算 +DM 和 -DM
            previous_high = previous.get('high_price', 0)
            previous_low = previous.get('low_price', 0)
            
            up_move = current_high - previous_high
            down_move = previous_low - current_low
            
            if up_move > down_move and up_move > 0:
                plus_dm.append(up_move)
                minus_dm.append(0)
            elif down_move > up_move and down_move > 0:
                plus_dm.append(0)
                minus_dm.append(down_move)
            else:
                plus_dm.append(0)
                minus_dm.append(0)
        
        # 计算平滑的 +DM, -DM 和 TR
        smoothed_plus_dm = sum(plus_dm[:period]) / period
        smoothed_minus_dm = sum(minus_dm[:period]) / period
        smoothed_tr = sum(true_ranges[:period]) / period
        
        # 计算 +DI 和 -DI
        plus_di_values = []
        minus_di_values = []
        
        for i in range(period, len(plus_dm)):
            smoothed_plus_dm = (
                smoothed_plus_dm - smoothed_plus_dm / period + plus_dm[i]
            )
            smoothed_minus_dm = (
                smoothed_minus_dm - smoothed_minus_dm / period + minus_dm[i]
            )
            smoothed_tr = smoothed_tr - smoothed_tr / period + true_ranges[i]
            
            if smoothed_tr > 0:
                plus_di = (smoothed_plus_dm / smoothed_tr) * 100
                minus_di = (smoothed_minus_dm / smoothed_tr) * 100
            else:
                plus_di = 0
                minus_di = 0
            
            plus_di_values.append(plus_di)
            minus_di_values.append(minus_di)
        
        # 计算 DX
        dx_values = []
        for i in range(len(plus_di_values)):
            di_sum = plus_di_values[i] + minus_di_values[i]
            if di_sum > 0:
                dx = abs(plus_di_values[i] - minus_di_values[i]) / di_sum * 100
            else:
                dx = 0
            dx_values.append(dx)
        
        # 计算 ADX（DX 的平滑平均）
        if len(dx_values) < period:
            return None
        
        adx = sum(dx_values[-period:]) / period
        return adx
    
    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> Optional[float]:
        """
        计算指数移动平均线 (EMA)
        
        Args:
            prices: 价格列表
            period: 周期
            
        Returns:
            EMA 值
        """
        if len(prices) < period:
            return None
        
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
    
    @staticmethod
    def calculate_atr(
        klines: List[Dict], 
        period: int = 14
    ) -> Optional[float]:
        """
        计算平均真实波幅 (ATR)
        
        Args:
            klines: K线数据列表
            period: 周期
            
        Returns:
            ATR 值
        """
        if len(klines) < period + 1:
            return None
        
        true_ranges = []
        for i in range(1, len(klines)):
            current = klines[i]
            previous = klines[i - 1]
            
            current_high = current.get('high_price', 0)
            current_low = current.get('low_price', 0)
            previous_close = previous.get('close_price', 0)
            
            high_low = current_high - current_low
            high_close = abs(current_high - previous_close)
            low_close = abs(current_low - previous_close)
            
            tr = max(high_low, high_close, low_close)
            true_ranges.append(tr)
        
        # 计算 ATR
        atr = sum(true_ranges[-period:]) / period
        return atr
    
    @staticmethod
    def calculate_all_indicators(klines: List[Dict]) -> Dict:
        """
        计算所有技术指标
        
        Args:
            klines: K线数据列表
            
        Returns:
            技术指标字典
        """
        if not klines or len(klines) < 30:
            return {}
        
        close_prices = [k.get('close_price', 0) for k in klines]
        
        indicators = {
            'adx': TechnicalIndicators.calculate_adx(klines, 14),
            'ema_fast': TechnicalIndicators.calculate_ema(close_prices, 20),
            'ema_slow': TechnicalIndicators.calculate_ema(close_prices, 50),
            'atr': TechnicalIndicators.calculate_atr(klines, 14),
        }
        
        return indicators
