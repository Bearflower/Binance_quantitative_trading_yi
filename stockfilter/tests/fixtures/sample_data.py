"""
测试夹具 - 模拟数据
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def create_bullish_pattern_stock():
    """
    创建一个符合看涨形态的模拟股票数据
    形态特征：大跌→缩量→放量→回踩
    """
    np.random.seed(42)
    
    days = 120
    dates = pd.date_range(end=datetime.now(), periods=days, freq='B')
    
    prices = []
    volumes = []
    
    base_price = 100
    
    for i in range(days):
        if i < 20:
            price = base_price * (1 - i * 0.015)
            volume = np.random.randint(500000, 800000)
        elif i < 40:
            price = base_price * 0.7 + np.random.randn() * 2
            volume = np.random.randint(200000, 400000)
        elif i < 50:
            price = base_price * 0.7 * (1 + (i-40) * 0.02)
            volume = np.random.randint(800000, 1200000) if i == 45 else np.random.randint(300000, 500000)
        elif i < 60:
            price = base_price * 0.7 * 1.2 * (1 - (i-50) * 0.005)
            volume = np.random.randint(200000, 400000)
        else:
            price = base_price * 0.7 * 1.2 * 0.97 + np.random.randn() * 3
            volume = np.random.randint(400000, 600000)
        
        prices.append(price)
        volumes.append(volume)
    
    prices = np.array(prices)
    open_price = prices * (1 + np.random.randn(days) * 0.01)
    high = np.maximum(open_price, prices) * (1 + np.abs(np.random.randn(days) * 0.01))
    low = np.minimum(open_price, prices) * (1 - np.abs(np.random.randn(days) * 0.01))
    
    df = pd.DataFrame({
        'date': dates,
        'open': open_price,
        'high': high,
        'low': low,
        'close': prices,
        'volume': volumes
    })
    
    return df


def create_random_stock_data(days=120, base_price=100):
    """创建随机股票数据"""
    np.random.seed(123)
    
    dates = pd.date_range(end=datetime.now(), periods=days, freq='B')
    
    returns = np.random.randn(days) * 0.02
    close = base_price * np.cumprod(1 + returns)
    open_price = close * (1 + np.random.randn(days) * 0.01)
    high = np.maximum(open_price, close) * (1 + np.abs(np.random.randn(days) * 0.01))
    low = np.minimum(open_price, close) * (1 - np.abs(np.random.randn(days) * 0.01))
    volume = np.random.randint(100000, 1000000, days)
    
    df = pd.DataFrame({
        'date': dates,
        'open': open_price,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    })
    
    return df


if __name__ == '__main__':
    df = create_bullish_pattern_stock()
    print(df.head(10))
    print(df.tail(10))
