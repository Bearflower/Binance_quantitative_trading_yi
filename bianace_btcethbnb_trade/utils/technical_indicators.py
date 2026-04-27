#!/usr/bin/env python3
"""
技术指标计算模块
从通用 K 线服务获取 K 线数据并计算技术指标
"""

import requests
import numpy as np
import pandas as pd
from datetime import datetime

KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"

def get_binance_klines(symbol="BTCUSDT", interval="1h", limit=100):
    """
    从通用 K 线服务获取 Binance K 线数据
    """
    url = f"{KLINE_SERVICE_URL}/klines/latest?symbol={symbol}&interval={interval}&limit={limit}"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                return result['data']
            else:
                print(f"获取 K 线数据失败：{result.get('message')}")
                return None
        else:
            print(f"获取 K 线数据失败：{response.status_code}")
            return None
    except Exception as e:
        print(f"获取 K 线数据错误：{str(e)}")
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
            print(f"获取资金费率失败：{response.status_code}")
            return None
    except Exception as e:
        print(f"获取资金费率错误：{str(e)}")
        return None

def calculate_ema(prices: pd.Series, period: int) -> pd.Series:
    """计算指数移动平均线"""
    return prices.ewm(span=period, adjust=False).mean()

def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """计算平均真实波动幅度"""
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """计算相对强弱指标"""
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """计算 MACD 指标"""
    exp1 = close.ewm(span=fast, adjust=False).mean()
    exp2 = close.ewm(span=slow, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def calculate_bollinger_bands(close: pd.Series, period: int = 20, std_dev: float = 2.0) -> tuple:
    """计算布林带"""
    middle = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = middle + (std * std_dev)
    lower = middle - (std * std_dev)
    return upper, middle, lower

def calculate_kdj(high: pd.Series, low: pd.Series, close: pd.Series, 
                  n: int = 9, m1: int = 3, m2: int = 3) -> tuple:
    """计算 KDJ 指标"""
    lowest_low = low.rolling(window=n).min()
    highest_high = high.rolling(window=n).max()
    rsv = (close - lowest_low) / (highest_high - lowest_low) * 100
    k = rsv.ewm(span=m1, adjust=False).mean()
    d = k.ewm(span=m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j

def calculate_cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """计算 CCI 指标"""
    tp = (high + low + close) / 3
    sma = tp.rolling(window=period).mean()
    mad = tp.rolling(window=period).apply(lambda x: np.abs((x - x.mean()).mean()))
    cci = (tp - sma) / (0.015 * mad)
    return cci

def calculate_rsi_2(close: pd.Series) -> pd.Series:
    """计算 2 日 RSI"""
    return calculate_rsi(close, 2)

def calculate_rsi_3(close: pd.Series) -> pd.Series:
    """计算 3 日 RSI"""
    return calculate_rsi(close, 3)

def calculate_rsi_4(close: pd.Series) -> pd.Series:
    """计算 4 日 RSI"""
    return calculate_rsi(close, 4)

def calculate_rsi_6(close: pd.Series) -> pd.Series:
    """计算 6 日 RSI"""
    return calculate_rsi(close, 6)

def calculate_rsi_12(close: pd.Series) -> pd.Series:
    """计算 12 日 RSI"""
    return calculate_rsi(close, 12)

def calculate_rsi_24(close: pd.Series) -> pd.Series:
    """计算 24 日 RSI"""
    return calculate_rsi(close, 24)

def calculate_rsi_25(close: pd.Series) -> pd.Series:
    """计算 25 日 RSI"""
    return calculate_rsi(close, 25)

def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算所有技术指标
    
    Args:
        df: 包含 OHLCV 数据的 DataFrame
    
    Returns:
        添加了技术指标的 DataFrame
    """
    # EMA
    df['ema2'] = calculate_ema(df['close'], 2)
    df['ema3'] = calculate_ema(df['close'], 3)
    df['ema4'] = calculate_ema(df['close'], 4)
    df['ema5'] = calculate_ema(df['close'], 5)
    df['ema6'] = calculate_ema(df['close'], 6)
    df['ema9'] = calculate_ema(df['close'], 9)
    df['ema10'] = calculate_ema(df['close'], 10)
    df['ema12'] = calculate_ema(df['close'], 12)
    df['ema18'] = calculate_ema(df['close'], 18)
    df['ema20'] = calculate_ema(df['close'], 20)
    df['ema21'] = calculate_ema(df['close'], 21)
    df['ema25'] = calculate_ema(df['close'], 25)
    df['ema30'] = calculate_ema(df['close'], 30)
    df['ema50'] = calculate_ema(df['close'], 50)
    df['ema100'] = calculate_ema(df['close'], 100)
    df['ema120'] = calculate_ema(df['close'], 120)
    df['ema150'] = calculate_ema(df['close'], 150)
    df['ema200'] = calculate_ema(df['close'], 200)
    df['ema250'] = calculate_ema(df['close'], 250)
    
    # ATR
    df['atr'] = calculate_atr(df['high'], df['low'], df['close'], 14)
    
    # RSI
    df['rsi_2'] = calculate_rsi_2(df['close'])
    df['rsi_3'] = calculate_rsi_3(df['close'])
    df['rsi_4'] = calculate_rsi_4(df['close'])
    df['rsi_6'] = calculate_rsi_6(df['close'])
    df['rsi_12'] = calculate_rsi_12(df['close'])
    df['rsi_24'] = calculate_rsi_24(df['close'])
    df['rsi_25'] = calculate_rsi_25(df['close'])
    
    # MACD
    df['macd'], df['macd_signal'], df['macd_hist'] = calculate_macd(df['close'])
    
    # 布林带
    df['bb_upper'], df['bb_middle'], df['bb_lower'] = calculate_bollinger_bands(df['close'])
    
    # KDJ
    df['k'], df['d'], df['j'] = calculate_kdj(df['high'], df['low'], df['close'])
    
    # CCI
    df['cci'] = calculate_cci(df['high'], df['low'], df['close'])
    
    return df
