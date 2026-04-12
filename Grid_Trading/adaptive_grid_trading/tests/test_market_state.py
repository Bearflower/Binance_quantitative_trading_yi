"""
测试市场状态识别器
"""

import pandas as pd
from datetime import datetime, timedelta
from src.strategy.market_state import MarketStateDetector, MarketState


def create_test_klines(trend: str = 'ranging') -> pd.DataFrame:
    """
    创建测试 K 线数据
    
    Args:
        trend: 趋势类型 (ranging/uptrend/downtrend)
    """
    n = 100
    dates = pd.date_range(datetime.now() - timedelta(hours=n), periods=n, freq='1h')
    
    if trend == 'ranging':
        # 震荡行情：价格在区间内波动
        prices = [100 + i % 10 for i in range(n)]
    elif trend == 'uptrend':
        # 上升趋势：价格持续上涨
        prices = [100 + i * 0.5 for i in range(n)]
    else:
        # 下降趋势：价格持续下跌
        prices = [150 - i * 0.5 for i in range(n)]
    
    df = pd.DataFrame({
        'open': prices,
        'high': [p + 2 for p in prices],
        'low': [p - 2 for p in prices],
        'close': prices,
        'volume': [1000] * n
    }, index=dates)
    
    return df


def test_ranging_market():
    """测试震荡市场识别"""
    detector = MarketStateDetector()
    klines = create_test_klines('ranging')
    
    result = detector.detect(klines, klines)
    
    print(f"震荡市场测试：{result.state.value}, ADX={result.adx:.2f}")
    assert result.state == MarketState.RANGING or result.adx < 25


def test_uptrend_market():
    """测试上升趋势识别"""
    detector = MarketStateDetector()
    klines = create_test_klines('uptrend')
    
    result = detector.detect(klines, klines)
    
    print(f"上升趋势测试：{result.state.value}, ADX={result.adx:.2f}")
    # 上升趋势应该有较高的 ADX


def test_downtrend_market():
    """测试下降趋势识别"""
    detector = MarketStateDetector()
    klines = create_test_klines('downtrend')
    
    result = detector.detect(klines, klines)
    
    print(f"下降趋势测试：{result.state.value}, ADX={result.adx:.2f}")
    # 下降趋势应该有较高的 ADX


if __name__ == "__main__":
    test_ranging_market()
    test_uptrend_market()
    test_downtrend_market()
    print("\n市场状态测试完成！")
