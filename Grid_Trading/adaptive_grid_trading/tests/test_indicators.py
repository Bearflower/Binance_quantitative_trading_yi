"""
测试技术指标计算
"""

import pandas as pd
import numpy as np
from src.data.indicators import TechnicalIndicators


def test_ema():
    """测试 EMA 计算"""
    # 创建测试数据
    prices = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    
    # 计算 EMA
    ema = TechnicalIndicators.ema(prices, period=3)
    
    assert len(ema) == len(prices)
    assert not ema.isna().any()
    print(f"EMA 计算正确：{ema.iloc[-1]:.4f}")


def test_atr():
    """测试 ATR 计算"""
    # 创建测试数据
    high = pd.Series([10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
    low = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    close = pd.Series([5, 6, 7, 8, 9, 10, 11, 12, 13, 14])
    
    # 计算 ATR
    atr = TechnicalIndicators.atr(high, low, close, period=5)
    
    assert len(atr) == len(high)
    print(f"ATR 计算正确：{atr.iloc[-1]:.4f}")


def test_adx():
    """测试 ADX 计算"""
    # 创建测试数据
    high = pd.Series([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25])
    low = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16])
    close = pd.Series([5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20])
    
    # 计算 ADX
    adx = TechnicalIndicators.adx(high, low, close, period=14)
    
    assert len(adx) == len(high)
    print(f"ADX 计算正确：{adx.iloc[-1]:.4f}")


def test_calculate_all_indicators():
    """测试全部指标计算"""
    # 创建测试 K 线数据
    df = pd.DataFrame({
        'open': range(100, 116),
        'high': range(105, 121),
        'low': range(95, 111),
        'close': range(98, 114),
        'volume': [1000] * 16
    })
    df.index = pd.date_range('2024-01-01', periods=16, freq='1h')
    
    # 计算所有指标
    result = TechnicalIndicators.calculate_all_indicators(df)
    
    # 验证指标列存在
    assert 'ema_fast' in result.columns
    assert 'ema_slow' in result.columns
    assert 'atr' in result.columns
    assert 'adx' in result.columns
    
    print("所有指标计算正确")


if __name__ == "__main__":
    test_ema()
    test_atr()
    test_adx()
    test_calculate_all_indicators()
    print("\n所有测试通过！")
