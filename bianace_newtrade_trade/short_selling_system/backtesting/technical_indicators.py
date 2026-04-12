#!/usr/bin/env python3
"""
技术指标计算模块
包含：EMA, MACD, RSI, ATR, 布林带，Parabolic SAR 等
"""

from decimal import Decimal
from typing import List, Dict, Optional


def calculate_ema(data: List[Dict], period: int) -> Decimal:
    """计算 EMA"""
    if len(data) < period:
        return Decimal('0')
    
    multiplier = Decimal('2') / (Decimal(period) + Decimal('1'))
    
    ema = sum(Decimal(str(k['close'])) for k in data[:period]) / Decimal(period)
    
    for k in data[period:]:
        close = Decimal(str(k['close']))
        ema = (close - ema) * multiplier + ema
    
    return ema


def calculate_macd(data: List[Dict], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict:
    """计算 MACD"""
    if len(data) < slow + signal:
        return {'macd': Decimal('0'), 'signal': Decimal('0'), 'histogram': Decimal('0')}
    
    ema_fast = calculate_ema(data, fast)
    ema_slow = calculate_ema(data, slow)
    
    macd_line = ema_fast - ema_slow
    
    signal_line = macd_line * Decimal('0.9')
    histogram = macd_line - signal_line
    
    return {
        'macd': macd_line,
        'signal': signal_line,
        'histogram': histogram
    }


def calculate_rsi(data: List[Dict], period: int = 14) -> Decimal:
    """计算 RSI"""
    if len(data) < period + 1:
        return Decimal('50')
    
    gains = []
    losses = []
    
    for i in range(1, len(data)):
        change = Decimal(str(data[i]['close'])) - Decimal(str(data[i-1]['close']))
        if change > 0:
            gains.append(change)
            losses.append(Decimal('0'))
        else:
            gains.append(Decimal('0'))
            losses.append(abs(change))
    
    avg_gain = sum(gains[-period:]) / Decimal(period)
    avg_loss = sum(losses[-period:]) / Decimal(period)
    
    if avg_loss == 0:
        return Decimal('100')
    
    rs = avg_gain / avg_loss
    rsi = Decimal('100') - (Decimal('100') / (Decimal('1') + rs))
    
    return rsi


def calculate_atr(data: List[Dict], period: int = 14) -> Decimal:
    """计算 ATR (Average True Range)"""
    if len(data) < period + 1:
        return Decimal('0')
    
    tr_values = []
    
    for i in range(1, len(data)):
        high = Decimal(str(data[i]['high']))
        low = Decimal(str(data[i]['low']))
        prev_close = Decimal(str(data[i-1]['close']))
        
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        
        tr = max(tr1, tr2, tr3)
        tr_values.append(tr)
    
    atr = sum(tr_values[-period:]) / Decimal(period)
    return atr


def calculate_bollinger_bands(data: List[Dict], period: int = 20, std_dev: float = 2.0) -> Dict:
    """计算布林带"""
    if len(data) < period:
        return {'upper': Decimal('0'), 'middle': Decimal('0'), 'lower': Decimal('0')}
    
    closes = [Decimal(str(k['close'])) for k in data[-period:]]
    
    middle = sum(closes) / Decimal(len(closes))
    
    variance = sum((c - middle) ** 2 for c in closes) / Decimal(len(closes))
    std = variance.sqrt() if hasattr(variance, 'sqrt') else Decimal(str(float(variance) ** 0.5))
    
    std_dev_dec = Decimal(str(std_dev))
    upper = middle + (std_dev_dec * std)
    lower = middle - (std_dev_dec * std)
    
    return {
        'upper': upper,
        'middle': middle,
        'lower': lower
    }


def calculate_parabolic_sar(data: List[Dict], af_start: float = 0.02, af_max: float = 0.2) -> List[Decimal]:
    """计算 Parabolic SAR"""
    if len(data) < 20:
        return []
    
    sar_values = []
    af_start_dec = Decimal(str(af_start))
    af_max_dec = Decimal(str(af_max))
    
    highs = [Decimal(str(k['high'])) for k in data[:20]]
    lows = [Decimal(str(k['low'])) for k in data[:20]]
    
    if highs[-1] > highs[0]:
        uptrend = True
        sar = min(lows)
        ep = max(highs)
    else:
        uptrend = False
        sar = max(highs)
        ep = min(lows)
    
    af = af_start_dec
    
    for i in range(20, len(data)):
        high = Decimal(str(data[i]['high']))
        low = Decimal(str(data[i]['low']))
        close = Decimal(str(data[i]['close']))
        
        new_sar = sar + af * (ep - sar)
        
        if uptrend:
            prev_low = Decimal(str(data[i-1]['low']))
            prev2_low = Decimal(str(data[i-2]['low']))
            if new_sar > min(prev_low, prev2_low):
                new_sar = min(prev_low, prev2_low)
        else:
            prev_high = Decimal(str(data[i-1]['high']))
            prev2_high = Decimal(str(data[i-2]['high']))
            if new_sar < max(prev_high, prev2_high):
                new_sar = max(prev_high, prev2_high)
        
        sar = new_sar
        sar_values.append(sar)
        
        if uptrend and close < sar:
            uptrend = False
            sar = ep
            af = af_start_dec
        elif not uptrend and close > sar:
            uptrend = True
            sar = ep
            af = af_start_dec
        
        if uptrend and high > ep:
            ep = high
            af = min(af + af_start_dec, af_max_dec)
        elif not uptrend and low < ep:
            ep = low
            af = min(af + af_start_dec, af_max_dec)
    
    return sar_values


def calculate_ema_slope(data: List[Dict], period: int = 21, lookback: int = 10) -> Decimal:
    """计算 EMA 斜率（百分比）"""
    if len(data) < period + lookback:
        return Decimal('0')
    
    current_ema = calculate_ema(data[-period:], period)
    past_ema = calculate_ema(data[-(period+lookback):-lookback], period)
    
    if past_ema == 0:
        return Decimal('0')
    
    slope = ((current_ema - past_ema) / past_ema) * Decimal('100')
    return slope


def calculate_volume_ratio(data: List[Dict], period: int = 20) -> Decimal:
    """计算成交量比率"""
    if len(data) < period:
        return Decimal('1')
    
    current_vol = Decimal(str(data[-1]['volume']))
    avg_vol = sum(Decimal(str(k['volume'])) for k in data[-period:]) / Decimal(period)
    
    if avg_vol == 0:
        return Decimal('1')
    
    return current_vol / avg_vol


def is_bullish_engulfing(data: List[Dict]) -> bool:
    """检测阳包阴形态"""
    if len(data) < 2:
        return False
    
    prev_open = Decimal(str(data[-2]['open']))
    prev_close = Decimal(str(data[-2]['close']))
    curr_open = Decimal(str(data[-1]['open']))
    curr_close = Decimal(str(data[-1]['close']))
    
    if prev_close >= prev_open:
        return False
    
    if curr_close <= curr_open:
        return False
    
    return curr_open < prev_close and curr_close > prev_open


def is_bearish_engulfing(data: List[Dict]) -> bool:
    """检测阴包阳形态"""
    if len(data) < 2:
        return False
    
    prev_open = Decimal(str(data[-2]['open']))
    prev_close = Decimal(str(data[-2]['close']))
    curr_open = Decimal(str(data[-1]['open']))
    curr_close = Decimal(str(data[-1]['close']))
    
    if prev_close <= prev_open:
        return False
    
    if curr_close >= curr_open:
        return False
    
    return curr_open > prev_close and curr_close < prev_open


def calculate_ema_trend(data: List[Dict]) -> str:
    """
    计算 EMA 趋势
    返回：'downtrend', 'uptrend', 'sideways'
    """
    if len(data) < 200:
        return 'sideways'
    
    ema5 = calculate_ema(data[-50:], 5)
    ema10 = calculate_ema(data[-50:], 10)
    ema21 = calculate_ema(data[-50:], 21)
    ema50 = calculate_ema(data[-50:], 50)
    ema200 = calculate_ema(data[-200:], 200)
    
    if ema200 == 0:
        return 'sideways'
    
    if ema5 < ema10 < ema21 < ema50 < ema200:
        return 'downtrend'
    elif ema5 > ema10 > ema21 > ema50 > ema200:
        return 'uptrend'
    else:
        return 'sideways'


def calculate_price_change(data: List[Dict], periods: int = 20) -> Decimal:
    """计算价格变化率"""
    if len(data) < periods:
        return Decimal('0')
    
    current_price = Decimal(str(data[-1]['close']))
    past_price = Decimal(str(data[-periods]['close']))
    
    if past_price == 0:
        return Decimal('0')
    
    return ((current_price - past_price) / past_price) * Decimal('100')
