#!/usr/bin/env python3
"""
技术指标计算模块（v5.0 规则引擎强化版）

实现完整的技术指标计算，支持多时间框架
"""

from decimal import Decimal
from typing import List, Dict, Any
import logging

logger = logging.getLogger(__name__)


def calculate_ema(data: List[Dict], period: int = 21) -> Decimal:
    """
    计算 EMA（指数移动平均线）
    
    Args:
        data: K 线数据列表（按时间正序）
        period: EMA 周期
    
    Returns:
        最新 EMA 值
    """
    if len(data) < period:
        return Decimal(str(data[-1]['close'])) if data else Decimal('0')
    
    # 使用简单 EMA 计算公式
    multiplier = Decimal('2') / (Decimal(period) + Decimal('1'))
    
    # 计算第一个 SMA
    sum_prices = sum(Decimal(str(k['close'])) for k in data[:period])
    ema = sum_prices / Decimal(period)
    
    # 计算后续 EMA
    for k in data[period:]:
        close = Decimal(str(k['close']))
        ema = (close - ema) * multiplier + ema
    
    return ema


def calculate_ema_slope(data: List[Dict], period: int = 21, lookback: int = 5) -> Decimal:
    """
    计算 EMA 斜率（线性回归）
    
    Args:
        data: K 线数据列表
        period: EMA 周期
        lookback: 回看 K 线数量计算斜率
    
    Returns:
        斜率（百分比）
    """
    if len(data) < lookback:
        return Decimal('0')
    
    # 计算最近 lookback 根 K 线的 EMA
    emas = []
    for i in range(len(data) - lookback, len(data)):
        subset = data[:i+1]
        if len(subset) >= period:
            ema = calculate_ema(subset, period)
            emas.append(ema)
    
    if len(emas) < 2:
        return Decimal('0')
    
    # 简单线性回归斜率
    slope = (emas[-1] - emas[0]) / emas[0] / Decimal(lookback)
    return slope * Decimal('100')  # 转换为百分比


def calculate_macd(data: List[Dict], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, Decimal]:
    """
    计算 MACD
    
    Returns:
        {'macd': ..., 'signal': ..., 'histogram': ...}
    """
    if len(data) < slow + signal:
        return {'macd': Decimal('0'), 'signal': Decimal('0'), 'histogram': Decimal('0')}
    
    # 计算快慢 EMA
    ema_fast = calculate_ema(data, fast)
    ema_slow = calculate_ema(data, slow)
    
    macd_line = ema_fast - ema_slow
    
    # 简化：signal 线使用最近 9 个 MACD 值的平均
    signal_line = macd_line * Decimal('0.9')  # 简化
    
    histogram = macd_line - signal_line
    
    return {
        'macd': macd_line,
        'signal': signal_line,
        'histogram': histogram
    }


def calculate_rsi(data: List[Dict], period: int = 14) -> Decimal:
    """
    计算 RSI（相对强弱指标）
    
    Returns:
        RSI 值（0-100）
    """
    if len(data) < period + 1:
        return Decimal('50')  # 中性值
    
    # 计算涨跌幅
    gains = []
    losses = []
    
    for i in range(len(data) - period, len(data)):
        prev_close = Decimal(str(data[i-1]['close']))
        curr_close = Decimal(str(data[i]['close']))
        change = curr_close - prev_close
        
        if change > 0:
            gains.append(change)
            losses.append(Decimal('0'))
        else:
            gains.append(Decimal('0'))
            losses.append(abs(change))
    
    if not gains or not losses:
        return Decimal('50')
    
    avg_gain = sum(gains) / Decimal(len(gains))
    avg_loss = sum(losses) / Decimal(len(losses))
    
    if avg_loss == Decimal('0'):
        return Decimal('100')
    
    rs = avg_gain / avg_loss
    rsi = Decimal('100') - (Decimal('100') / (Decimal('1') + rs))
    
    return rsi


def calculate_atr(data: List[Dict], period: int = 14) -> Decimal:
    """
    计算 ATR（平均真实波幅）
    
    Returns:
        ATR 值
    """
    if len(data) < period + 1:
        # 使用简单平均波幅
        if len(data) > 1:
            true_ranges = []
            for k in data[-period:]:
                high = Decimal(str(k['high']))
                low = Decimal(str(k['low']))
                true_ranges.append(high - low)
            return sum(true_ranges) / Decimal(len(true_ranges))
        return Decimal('0')
    
    # 计算真实波幅
    true_ranges = []
    for i in range(len(data) - period, len(data)):
        high = Decimal(str(data[i]['high']))
        low = Decimal(str(data[i]['low']))
        prev_close = Decimal(str(data[i-1]['close']))
        
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        
        true_ranges.append(max(tr1, tr2, tr3))
    
    atr = sum(true_ranges) / Decimal(len(true_ranges))
    return atr


def calculate_bollinger_bands(data: List[Dict], period: int = 20, std_dev: int = 2) -> Dict[str, Decimal]:
    """
    计算布林带
    
    Returns:
        {'upper': ..., 'middle': ..., 'lower': ...}
    """
    if len(data) < period:
        close = Decimal(str(data[-1]['close'])) if data else Decimal('0')
        return {'upper': close, 'middle': close, 'lower': close}
    
    # 计算中轨（SMA）
    closes = [Decimal(str(k['close'])) for k in data[-period:]]
    middle = sum(closes) / Decimal(period)
    
    # 计算标准差
    variance = sum((c - middle) ** 2 for c in closes) / Decimal(period)
    std = variance.sqrt() if variance > 0 else Decimal('0')
    
    upper = middle + Decimal(std_dev) * std
    lower = middle - Decimal(std_dev) * std
    
    return {
        'upper': upper,
        'middle': middle,
        'lower': lower
    }


def calculate_volume_ratio(data: List[Dict], period: int = 20) -> Decimal:
    """
    计算成交量比率（当前成交量 / 过去 period 小时平均成交量）
    
    Returns:
        成交量比率
    """
    if len(data) < period + 1:
        return Decimal('1.0')
    
    current_volume = Decimal(str(data[-1]['volume']))
    avg_volume = sum(Decimal(str(k['volume'])) for k in data[-period:-1]) / Decimal(period - 1)
    
    if avg_volume == Decimal('0'):
        return Decimal('1.0')
    
    return current_volume / avg_volume


def is_bullish_engulfing(data: List[Dict]) -> bool:
    """
    检测阳包阴形态
    
    Returns:
        是否阳包阴
    """
    if len(data) < 2:
        return False
    
    current = data[-1]
    prev = data[-2]
    
    curr_open = Decimal(str(current['open']))
    curr_close = Decimal(str(current['close']))
    curr_high = Decimal(str(current['high']))
    curr_low = Decimal(str(current['low']))
    
    prev_open = Decimal(str(prev['open']))
    prev_close = Decimal(str(prev['close']))
    prev_high = Decimal(str(prev['high']))
    prev_low = Decimal(str(prev['low']))
    
    # 当前为阳线，前一根为阴线
    if curr_close <= curr_open or prev_close >= prev_open:
        return False
    
    # 当前阳线实体完全覆盖阴线实体
    if curr_close > prev_open and curr_low < prev_low:
        return True
    
    return False


def is_bearish_engulfing(data: List[Dict]) -> bool:
    """
    检测阴包阳形态
    
    Returns:
        是否阴包阳
    """
    if len(data) < 2:
        return False
    
    current = data[-1]
    prev = data[-2]
    
    curr_open = Decimal(str(current['open']))
    curr_close = Decimal(str(current['close']))
    
    prev_open = Decimal(str(prev['open']))
    prev_close = Decimal(str(prev['close']))
    
    # 当前为阴线，前一根为阳线
    if curr_close >= curr_open or prev_close <= prev_open:
        return False
    
    # 当前阴线实体完全覆盖阳线实体
    if curr_close < prev_open:
        return True
    
    return False


def calculate_parabolic_sar(data: List[Dict], af_start: float = 0.02, af_max: float = 0.2) -> List[Decimal]:
    """
    计算 Parabolic SAR（抛物线转向指标）
    
    Args:
        data: K 线数据（按时间顺序）
        af_start: 初始加速因子（默认 0.02）
        af_max: 最大加速因子（默认 0.2）
    
    Returns:
        SAR 值列表
    """
    if len(data) < 20:
        return []
    
    sar_values = []
    
    # 初始化：找到第一个趋势
    # 简单方法：前 20 根 K 线的最高/最低点
    highs = [Decimal(str(k['high'])) for k in data[:20]]
    lows = [Decimal(str(k['low'])) for k in data[:20]]
    
    # 初始趋势判断
    if highs[-1] > highs[0]:
        uptrend = True
        sar = min(lows)
        ep = max(highs)
    else:
        uptrend = False
        sar = max(highs)
        ep = min(lows)
    
    af = Decimal(str(af_start))
    af_start_dec = Decimal(str(af_start))
    af_max_dec = Decimal(str(af_max))
    
    # 计算后续 SAR
    for i in range(20, len(data)):
        k = data[i]
        high = Decimal(str(k['high']))
        low = Decimal(str(k['low']))
        close = Decimal(str(k['close']))
        
        # 计算新 SAR
        new_sar = sar + af * (ep - sar)
        
        # SAR 不能超过前两根 K 线的极值
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
        
        # 检查趋势反转
        if uptrend and close < sar:
            uptrend = False
            sar = ep  # 切换到前高
            af = af_start_dec
            ep = min(low, lows[-1] if 'lows' in dir() else low)
        elif not uptrend and close > sar:
            uptrend = True
            sar = ep  # 切换到前低
            af = af_start_dec
            ep = max(high, highs[-1] if 'highs' in dir() else high)
        else:
            # 更新极值点和加速因子
            if uptrend and high > ep:
                ep = high
                af = min(af + af_start_dec, af_max_dec)
            elif not uptrend and low < ep:
                ep = low
                af = min(af + af_start_dec, af_max_dec)
    
    return sar_values


def check_macd_divergence(data: List[Dict], direction: int) -> bool:
    """
    检测 MACD 背离
    
    Args:
        data: K 线数据
        direction: 1=底背离（做多），-1=顶背离（做空）
    
    Returns:
        是否背离
    """
    if len(data) < 40:  # 需要至少 20 小时数据检测背离
        return False
    
    # 简化检测：比较最近 20 小时的 MACD
    recent_data = data[-40:]
    
    macd_current = calculate_macd(recent_data[-20:])['histogram']
    macd_prev = calculate_macd(recent_data[:20])['histogram']
    
    if direction == 1:  # 底背离：价格新低，MACD 抬高
        # 简化判断
        return macd_current > macd_prev
    else:  # 顶背离：价格新高，MACD 降低
        return macd_current < macd_prev


def calculate_all_indicators(klines_1h: List[Dict]) -> Dict[str, Any]:
    """
    计算所有技术指标
    
    Returns:
        包含所有指标的字典
    """
    if not klines_1h:
        return {}
    
    # 1 小时指标
    ema21_1h = calculate_ema(klines_1h, 21)
    ema55_1h = calculate_ema(klines_1h, 55)
    atr14_1h = calculate_atr(klines_1h, 14)
    rsi14_1h = calculate_rsi(klines_1h, 14)
    bb_1h = calculate_bollinger_bands(klines_1h, 20)
    macd_1h = calculate_macd(klines_1h)
    volume_ratio_1h = calculate_volume_ratio(klines_1h, 20)
    
    # 形态检测
    bullish_engulfing = is_bullish_engulfing(klines_1h)
    bearish_engulfing = is_bearish_engulfing(klines_1h)
    macd_bullish_div = check_macd_divergence(klines_1h, 1)
    macd_bearish_div = check_macd_divergence(klines_1h, -1)
    
    # 最新价格
    current_price = Decimal(str(klines_1h[-1]['close']))
    
    return {
        '1h': {
            'ema21': ema21_1h,
            'ema55': ema55_1h,
            'atr14': atr14_1h,
            'rsi14': rsi14_1h,
            'bb_upper': bb_1h['upper'],
            'bb_middle': bb_1h['middle'],
            'bb_lower': bb_1h['lower'],
            'macd': macd_1h['macd'],
            'macd_signal': macd_1h['signal'],
            'macd_hist': macd_1h['histogram'],
            'volume_ratio': volume_ratio_1h,
            'bullish_engulfing': bullish_engulfing,
            'bearish_engulfing': bearish_engulfing,
            'macd_bullish_div': macd_bullish_div,
            'macd_bearish_div': macd_bearish_div,
        },
        'current_price': current_price
    }
