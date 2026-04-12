"""
技术指标计算器
提供 ADX、ATR、EMA 等技术指标的计算
"""

import logging
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class IndicatorCalculator:
    """技术指标计算器"""
    
    @staticmethod
    def calculate_ema(series: pd.Series, period: int) -> pd.Series:
        """
        计算指数移动平均线（EMA）
        
        Args:
            series: 价格序列
            period: EMA 周期
            
        Returns:
            EMA 序列
        """
        return series.ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def calculate_atr(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> pd.Series:
        """
        计算平均真实波幅（ATR）
        
        Args:
            high: 最高价序列
            low: 最低价序列
            close: 收盘价序列
            period: ATR 周期
            
        Returns:
            ATR 序列
        """
        # 计算真实波幅（TR）
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # 计算 ATR（使用 EMA 平滑）
        atr = tr.ewm(span=period, adjust=False).mean()
        
        return atr
    
    @staticmethod
    def calculate_smoothed_atr(
        atr: pd.Series,
        smoothing_period: int = 14
    ) -> pd.Series:
        """
        计算平滑 ATR（EMA of ATR）
        
        根据需求文档，使用 EMA 对 ATR 进行二次平滑
        
        Args:
            atr: ATR 序列
            smoothing_period: 平滑周期
            
        Returns:
            平滑 ATR 序列
        """
        return atr.ewm(span=smoothing_period, adjust=False).mean()
    
    @staticmethod
    def calculate_adx(
        high: pd.Series,
        low: pd.Series,
        close: pd.Series,
        period: int = 14
    ) -> pd.Series:
        """
        计算平均趋向指数（ADX）
        
        Args:
            high: 最高价序列
            low: 最低价序列
            close: 收盘价序列
            period: ADX 周期
            
        Returns:
            ADX 序列
        """
        # 计算 +DM 和 -DM
        high_diff = high.diff()
        low_diff = -low.diff()
        
        plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0)
        minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0)
        
        plus_dm = pd.Series(plus_dm, index=high.index)
        minus_dm = pd.Series(minus_dm, index=low.index)
        
        # 计算 TR
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # 计算平滑的 +DM, -DM, TR
        plus_dm_smooth = plus_dm.ewm(span=period, adjust=False).mean()
        minus_dm_smooth = minus_dm.ewm(span=period, adjust=False).mean()
        tr_smooth = tr.ewm(span=period, adjust=False).mean()
        
        # 计算 +DI 和 -DI
        plus_di = 100 * (plus_dm_smooth / tr_smooth)
        minus_di = 100 * (minus_dm_smooth / tr_smooth)
        
        # 计算 DX
        di_sum = plus_di + minus_di
        di_diff = abs(plus_di - minus_di)
        dx = 100 * (di_diff / di_sum)
        
        # 计算 ADX（DX 的 EMA）
        adx = dx.ewm(span=period, adjust=False).mean()
        
        return adx
    
    @staticmethod
    def calculate_market_state(
        adx: float,
        ema_fast: float,
        ema_slow: float,
        prev_state: str = None,
        adx_4h: float = None,
        ema_fast_4h: float = None,
        ema_slow_4h: float = None
    ) -> str:
        """
        判断市场状态
        
        Args:
            adx: 1H ADX 值
            ema_fast: 1H 快线 EMA
            ema_slow: 1H 慢线 EMA
            prev_state: 前一状态
            adx_4h: 4H ADX 值（多时间框架确认）
            ema_fast_4h: 4H 快线 EMA
            ema_slow_4h: 4H 慢线 EMA
            
        Returns:
            市场状态：'ranging'（震荡）, 'uptrend'（上升趋势）, 'downtrend'（下降趋势）
        """
        # 根据需求文档的状态判断逻辑
        if adx < 20:
            state = 'ranging'
        elif adx > 25:
            if ema_fast > ema_slow:
                state = 'uptrend'
            else:
                state = 'downtrend'
        else:
            # 20 ≤ ADX ≤ 25，保持前一状态
            state = prev_state if prev_state else 'ranging'
        
        # 多时间框架确认（如果提供了 4H 数据）
        if adx_4h is not None and ema_fast_4h is not None and ema_slow_4h is not None:
            # 4H 周期状态判断
            if adx_4h < 20:
                state_4h = 'ranging'
            elif adx_4h > 25:
                if ema_fast_4h > ema_slow_4h:
                    state_4h = 'uptrend'
                else:
                    state_4h = 'downtrend'
            else:
                state_4h = state  # 4H 弱趋势区，使用 1H 状态
            
            # 只有 1H 和 4H 状态一致才确认
            if state_4h != state:
                logger.info(f"多时间框架状态不一致：1H={state}, 4H={state_4h}，使用 1H 状态")
                # 这里选择保持 1H 状态，也可以根据策略调整
        
        return state
    
    @staticmethod
    def calculate_trend_strength(adx: float) -> float:
        """
        计算趋势强度系数
        
        根据需求文档：k_trend = min(0.5, max(0, (ADX - 25) / 30))
        
        Args:
            adx: ADX 值
            
        Returns:
            趋势强度系数（0-0.5）
        """
        k_trend = (adx - 25) / 30
        return max(0, min(0.5, k_trend))
    
    @staticmethod
    def calculate_all_indicators(
        df: pd.DataFrame,
        adx_period: int = 14,
        ema_fast_period: int = 20,
        ema_slow_period: int = 50,
        atr_period: int = 14,
        atr_smoothing: int = 14
    ) -> pd.DataFrame:
        """
        计算所有技术指标
        
        Args:
            df: K 线数据 DataFrame，需包含 'high', 'low', 'close' 列
            adx_period: ADX 周期
            ema_fast_period: 快线 EMA 周期
            ema_slow_period: 慢线 EMA 周期
            atr_period: ATR 周期
            atr_smoothing: ATR 平滑周期
            
        Returns:
            添加指标后的 DataFrame
        """
        # 复制 DataFrame，避免修改原数据
        result = df.copy()
        
        # 计算 EMA
        result['ema_fast'] = IndicatorCalculator.calculate_ema(
            result['close'], ema_fast_period
        )
        result['ema_slow'] = IndicatorCalculator.calculate_ema(
            result['close'], ema_slow_period
        )
        
        # 计算 ATR 和平滑 ATR
        result['atr'] = IndicatorCalculator.calculate_atr(
            result['high'], result['low'], result['close'], atr_period
        )
        result['atr_smooth'] = IndicatorCalculator.calculate_smoothed_atr(
            result['atr'], atr_smoothing
        )
        
        # 计算 ADX
        result['adx'] = IndicatorCalculator.calculate_adx(
            result['high'], result['low'], result['close'], adx_period
        )
        
        # 计算趋势强度系数
        result['trend_strength'] = result['adx'].apply(
            IndicatorCalculator.calculate_trend_strength
        )
        
        logger.info(f"指标计算完成，共 {len(result)} 条数据")
        
        return result
