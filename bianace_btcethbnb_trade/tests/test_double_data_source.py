#!/usr/bin/env python3
"""
双数据源对比测试
对比币安 API 原始数据 vs K 线服务数据
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data import MarketDataFetcher
import pandas as pd
import logging
from decimal import Decimal
import time
import requests
from typing import Dict

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_binance_klines(symbol: str, interval: str = '1h', limit: int = 100) -> pd.DataFrame:
    """
    从币安 API 获取原始 K 线数据（公开接口，无需 API Key）
    
    Args:
        symbol: 交易对
        interval: 时间间隔
        limit: 获取数量
    
    Returns:
        DataFrame 格式的 K 线数据
    """
    url = 'https://fapi.binance.com/fapi/v1/klines'
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        klines = response.json()
        
        df = pd.DataFrame(klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_vol', 'trades', 'taker_buy_vol', 'taker_buy_quote', 'ignore'
        ])
        
        # 转换数据类型
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        
        return df
        
    except Exception as e:
        logger.error(f"币安 API 错误：{e}")
        return None


def calculate_indicators_from_binance(df: pd.DataFrame) -> Dict:
    """
    从币安原始数据计算技术指标（简单实现）
    
    Args:
        df: 币安 K 线数据
    
    Returns:
        指标字典
    """
    indicators = {}
    
    # EMA21
    indicators['ema21'] = df['close'].ewm(span=21, adjust=False).mean().iloc[-1]
    
    # ATR14 (简化计算：使用真实波幅的平均值)
    high = df['high']
    low = df['low']
    close = df['close']
    
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    indicators['atr14'] = tr.rolling(window=14).mean().iloc[-1]
    
    # RSI14
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    indicators['rsi14'] = 100 - (100 / (1 + rs)).iloc[-1]
    
    return indicators


def get_kline_service_data(symbol: str) -> Dict:
    """
    从 K 线服务获取数据
    
    Args:
        symbol: 交易对
    
    Returns:
        K 线服务返回的数据
    """
    fetcher = MarketDataFetcher()
    data = fetcher.fetch_market_data([symbol])
    return data.get(symbol, {})


def compare_data(symbol: str):
    """
    对比币安数据和 K 线服务数据
    
    Args:
        symbol: 交易对
    """
    logger.info(f"{'='*80}")
    logger.info(f"开始对比 {symbol} 数据")
    logger.info(f"{'='*80}")
    
    # 1. 获取币安原始数据
    logger.info(f"\n1. 从币安 API 获取原始数据...")
    binance_1h = get_binance_klines(symbol, '1h', limit=100)
    
    if binance_1h is None or len(binance_1h) == 0:
        logger.error("无法获取币安数据")
        return
    
    logger.info(f"✅ 币安数据：{len(binance_1h)} 条")
    logger.info(f"最新价格：{binance_1h['close'].iloc[-1]:.2f}")
    
    # 2. 计算币安数据的技术指标
    logger.info(f"\n2. 计算币安数据技术指标...")
    binance_indicators = calculate_indicators_from_binance(binance_1h)
    
    logger.info(f"EMA21: {binance_indicators['ema21']:.2f}")
    logger.info(f"ATR14: {binance_indicators['atr14']:.2f}")
    logger.info(f"RSI14: {binance_indicators['rsi14']:.2f}")
    
    # 3. 获取 K 线服务数据
    logger.info(f"\n3. 从 K 线服务获取数据...")
    kline_data = get_kline_service_data(symbol)
    
    if not kline_data:
        logger.error("无法获取 K 线服务数据")
        return
    
    logger.info(f"✅ K 线服务数据获取成功")
    logger.info(f"最新价格：{kline_data.get('last_price', 'N/A')}")
    
    # 4. 对比 K 线服务的技术指标
    logger.info(f"\n4. 对比技术指标...")
    kline_indicators = kline_data.get('indicators', {}).get('1h', {})
    
    logger.info(f"EMA21: {kline_indicators.get('ema21', 'N/A')}")
    logger.info(f"ATR14: {kline_indicators.get('atr14', 'N/A')}")
    logger.info(f"RSI: {kline_indicators.get('rsi', 'N/A')}")
    
    # 5. 计算差异
    logger.info(f"\n5. 差异分析:")
    
    # 价格对比
    binance_price = float(binance_1h['close'].iloc[-1])
    kline_price = float(kline_data.get('last_price', 0))
    price_diff = abs(binance_price - kline_price)
    price_diff_pct = (price_diff / binance_price) * 100 if binance_price > 0 else 0
    
    logger.info(f"价格差异：{price_diff:.4f} ({price_diff_pct:.4f}%)")
    
    # EMA 对比
    if kline_indicators.get('ema21') is not None:
        ema_diff = abs(binance_indicators['ema21'] - float(kline_indicators['ema21']))
        ema_diff_pct = (ema_diff / binance_indicators['ema21']) * 100
        logger.info(f"EMA21 差异：{ema_diff:.4f} ({ema_diff_pct:.4f}%)")
    else:
        logger.warning("K 线服务 EMA21 为 None")
    
    # ATR 对比
    if kline_indicators.get('atr14') is not None:
        atr_diff = abs(binance_indicators['atr14'] - float(kline_indicators['atr14']))
        atr_diff_pct = (atr_diff / binance_indicators['atr14']) * 100
        logger.info(f"ATR14 差异：{atr_diff:.4f} ({atr_diff_pct:.4f}%)")
    else:
        logger.warning("K 线服务 ATR14 为 None")
    
    # RSI 对比
    if kline_indicators.get('rsi') is not None:
        rsi_diff = abs(binance_indicators['rsi14'] - float(kline_indicators['rsi']))
        logger.info(f"RSI14 差异：{rsi_diff:.4f}")
        
        # RSI 有效性检查
        if rsi_diff > 5:
            logger.error(f"⚠️ RSI 差异过大！> 5")
        elif rsi_diff > 2:
            logger.warning(f"⚠️ RSI 差异较大！> 2")
        else:
            logger.info(f"✅ RSI 差异在合理范围内")
    else:
        logger.error("K 线服务 RSI 为 None ❌")
    
    # 6. 结论
    logger.info(f"\n{'='*80}")
    logger.info(f"对比结论:")
    logger.info(f"{'='*80}")
    
    issues = []
    
    if price_diff_pct > 0.1:
        issues.append(f"价格差异过大：{price_diff_pct:.4f}%")
    
    if kline_indicators.get('rsi') is None:
        issues.append("RSI 指标缺失")
    
    if kline_indicators.get('ema21') is None:
        issues.append("EMA 指标缺失")
    
    if kline_indicators.get('atr14') is None:
        issues.append("ATR 指标缺失")
    
    if issues:
        logger.error(f"❌ 发现问题:")
        for issue in issues:
            logger.error(f"  - {issue}")
    else:
        logger.info(f"✅ 数据一致性良好")
    
    logger.info(f"{'='*80}\n")


def main():
    """主函数"""
    logger.info(f"\n{'='*80}")
    logger.info(f"双数据源对比测试")
    logger.info(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*80}\n")
    
    symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
    
    for symbol in symbols:
        compare_data(symbol)
        time.sleep(1)  # 避免 API 限流
    
    logger.info(f"\n{'='*80}")
    logger.info(f"所有交易对对比完成")
    logger.info(f"{'='*80}\n")


if __name__ == '__main__':
    main()
