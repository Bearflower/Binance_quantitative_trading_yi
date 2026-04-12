#!/usr/bin/env python3
"""
从币安 API 获取历史 K 线数据

获取近 6 个月的 BTCUSDT 多周期数据，用于本地回测
"""

import requests
import pandas as pd
import json
from datetime import datetime, timedelta
import time
from pathlib import Path

# 币安 API 基础 URL
BINANCE_API = "https://fapi.binance.com"

# 支持的时间周期
TIMEFRAMES = {
    '15m': '15m',
    '1h': '1h', 
    '4h': '4h',
    '1d': '1d'
}

def fetch_klines(symbol, interval, start_date, end_date):
    """
    获取 K 线数据
    
    Args:
        symbol: 交易对，如 'BTCUSDT'
        interval: 时间周期，如 '1h'
        start_date: 开始日期，datetime 对象
        end_date: 结束日期，datetime 对象
    
    Returns:
        DataFrame 格式的 K 线数据
    """
    print(f"正在获取 {symbol} {interval} 数据...")
    print(f"时间范围：{start_date} 至 {end_date}")
    
    all_klines = []
    current_time = start_date
    limit = 1000  # 每次最多获取 1000 条
    
    while current_time < end_date:
        # 计算本次请求的开始时间（毫秒）
        start_ms = int(current_time.timestamp() * 1000)
        
        # 构建 API 请求
        url = f"{BINANCE_API}/fapi/v1/klines"
        params = {
            'symbol': symbol,
            'interval': interval,
            'startTime': start_ms,
            'limit': limit
        }
        
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            
            klines = response.json()
            
            if not klines:
                break
            
            all_klines.extend(klines)
            
            # 更新当前时间（最后一条 K 线的结束时间）
            last_kline = klines[-1]
            current_time = datetime.fromtimestamp(last_kline[6] / 1000)
            
            print(f"  已获取 {len(all_klines)} 条数据，当前时间：{current_time}")
            
            # 避免触发 API 限流
            time.sleep(0.5)
            
        except Exception as e:
            print(f"获取数据失败：{e}")
            time.sleep(5)  # 失败后等待更长时间
    
    # 转换为 DataFrame
    if all_klines:
        df = pd.DataFrame(all_klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_volume',
            'taker_buy_quote_volume', 'ignore'
        ])
        
        # 转换时间戳
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        df['close_time'] = pd.to_datetime(df['close_time'], unit='ms')
        
        # 转换数值类型
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
        
        print(f"✅ {interval} 数据获取完成，共 {len(df)} 条")
        return df
    else:
        print(f"❌ {interval} 数据获取失败")
        return None


def fetch_multi_timeframe_data(symbol='BTCUSDT', months=6):
    """
    获取多周期 K 线数据
    
    Args:
        symbol: 交易对
        months: 获取月数
    
    Returns:
        包含多周期数据的字典
    """
    print("=" * 80)
    print(f"开始获取 {symbol} 近{months}个月的历史数据")
    print("=" * 80)
    
    # 计算时间范围
    end_date = datetime.now()
    start_date = end_date - timedelta(days=months * 30)
    
    print(f"时间范围：{start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}")
    print("=" * 80)
    
    # 获取各周期数据
    multi_timeframe_data = {}
    
    for timeframe_name, timeframe_code in TIMEFRAMES.items():
        print(f"\n获取 {timeframe_name} 周期数据...")
        df = fetch_klines(symbol, timeframe_code, start_date, end_date)
        
        if df is not None:
            multi_timeframe_data[timeframe_name] = df
            
            # 保存为 CSV
            output_file = Path(f'data/binance_{symbol}_{timeframe_name}_{months}m.csv')
            output_file.parent.mkdir(exist_ok=True)
            df.to_csv(output_file, index=False)
            print(f"  数据已保存：{output_file}")
    
    print("\n" + "=" * 80)
    print("数据获取完成！")
    print("=" * 80)
    
    # 生成汇总报告
    summary = {
        'symbol': symbol,
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'months': months,
        'timeframes': {}
    }
    
    for timeframe_name, df in multi_timeframe_data.items():
        summary['timeframes'][timeframe_name] = {
            'total_bars': len(df),
            'first_bar': df['open_time'].iloc[0].isoformat(),
            'last_bar': df['open_time'].iloc[-1].isoformat(),
            'price_range': {
                'min': float(df['low'].min()),
                'max': float(df['high'].max())
            }
        }
    
    # 保存汇总报告
    summary_file = Path('data/binance_multi_timeframe_summary.json')
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    
    print(f"汇总报告已保存：{summary_file}")
    print("=" * 80)
    
    return multi_timeframe_data, summary


if __name__ == '__main__':
    # 获取近 6 个月的 BTCUSDT 数据
    data, summary = fetch_multi_timeframe_data(symbol='BTCUSDT', months=6)
    
    print("\n数据获取完成！")
    print(f"获取的周期：{list(data.keys())}")
    print(f"时间范围：{summary['start_date']} 至 {summary['end_date']}")
