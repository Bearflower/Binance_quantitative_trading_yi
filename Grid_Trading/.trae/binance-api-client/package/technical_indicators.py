#!/usr/bin/env python3
"""
技术指标计算模块
从 Binance API 获取 K线数据并计算技术指标
"""

import requests
import numpy as np
import pandas as pd
from datetime import datetime

def get_binance_klines(symbol="BTCUSDT", interval="1h", limit=100):
    """
    获取 Binance K线数据
    """
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"获取 K线数据失败: {response.status_code}")
            return None
    except Exception as e:
        print(f"获取 K线数据错误: {str(e)}")
        return None

def get_funding_rate(symbol="BTCUSDT"):
    """
    获取资金费率数据
    """
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=10"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"获取资金费率失败: {response.status_code}")
            return None
    except Exception as e:
        print(f"获取资金费率错误: {str(e)}")
        return None

def get_order_book(symbol="BTCUSDT", limit=50):
    """
    获取订单簿深度数据
    """
    url = f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit={limit}"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"获取订单簿失败: {response.status_code}")
            return None
    except Exception as e:
        print(f"获取订单簿错误: {str(e)}")
        return None

def calculate_ema(prices, period=21):
    """
    计算 EMA（指数移动平均线）
    """
    series = pd.Series(prices)
    return series.ewm(span=period, adjust=False).mean().tolist()

def calculate_bollinger_bands(prices, period=20, std_dev=2):
    """
    计算布林带
    """
    series = pd.Series(prices)
    sma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    
    upper_band = (sma + (std * std_dev)).tolist()
    lower_band = (sma - (std * std_dev)).tolist()
    
    return sma.tolist(), upper_band, lower_band

def calculate_macd(prices, fast_period=12, slow_period=26, signal_period=9):
    """
    计算 MACD
    """
    series = pd.Series(prices)
    
    ema_fast = series.ewm(span=fast_period, adjust=False).mean()
    ema_slow = series.ewm(span=slow_period, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line
    
    return macd_line.tolist(), signal_line.tolist(), histogram.tolist()

def calculate_rsi(prices, period=14):
    """
    计算 RSI（相对强弱指标）
    """
    series = pd.Series(prices)
    delta = series.diff()
    
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi.tolist()

def calculate_atr(highs, lows, closes, period=14):
    """
    计算 ATR（平均真实范围）
    """
    tr = []
    for i in range(1, len(closes)):
        true_range = max(highs[i] - lows[i], 
                       abs(highs[i] - closes[i-1]), 
                       abs(lows[i] - closes[i-1]))
        tr.append(true_range)
    
    atr = pd.Series(tr).rolling(window=period).mean().tolist()
    return atr

def get_technical_indicators(symbol="BTCUSDT"):
    """
    获取完整的技术指标数据
    """
    indicators = {
        "symbol": symbol,
        "timestamp": datetime.now().isoformat()
    }
    
    # 定义需要的K线周期
    timeframes = ["1d", "4h", "1h", "15m"]
    
    for timeframe in timeframes:
        # 设置不同时间周期的limit参数
        if timeframe == "1d":
            limit = 50
        elif timeframe == "4h":
            limit = 100
        elif timeframe == "1h":
            limit = 100
        else:  # 15m
            limit = 100
        
        # 获取K线数据
        klines = get_binance_klines(symbol, timeframe, limit)
        if klines:
            close_prices = [float(kline[4]) for kline in klines]
            high_prices = [float(kline[2]) for kline in klines]
            low_prices = [float(kline[3]) for kline in klines]
            
            # 计算指标
            indicators[timeframe] = {
                "prices": close_prices[-20:],
                "ema21": calculate_ema(close_prices)[-20:],
                "bollinger": {
                    "sma": calculate_bollinger_bands(close_prices)[0][-20:],
                    "upper": calculate_bollinger_bands(close_prices)[1][-20:],
                    "lower": calculate_bollinger_bands(close_prices)[2][-20:]
                },
                "rsi": calculate_rsi(close_prices)[-20:],
                "atr14": calculate_atr(high_prices, low_prices, close_prices)[-20:]
            }
    
    # 获取资金费率
    funding_rate = get_funding_rate(symbol)
    if funding_rate:
        indicators["funding_rate"] = funding_rate[0]
    
    # 获取订单簿深度
    order_book = get_order_book(symbol, 20)
    if order_book:
        indicators["order_book"] = {
            "bids": order_book["bids"][:5],  # 前5个买单
            "asks": order_book["asks"][:5]   # 前5个卖单
        }
    
    return indicators
