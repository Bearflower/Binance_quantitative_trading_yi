#!/usr/bin/env python3
"""
从币安获取多时间框架 K 线数据 - 简化版
"""

import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any
import requests

def fetch_binance_klines(
    symbol: str,
    interval: str,
    start_time: datetime,
    end_time: datetime,
    limit: int = 1000
) -> List[Dict[str, Any]]:
    """从币安获取历史 K 线数据"""
    url = 'https://fapi.binance.com/fapi/v1/klines'
    all_klines = []
    current_start = start_time
    
    while current_start < end_time:
        params = {
            'symbol': symbol,
            'interval': interval,
            'startTime': int(current_start.timestamp() * 1000),
            'endTime': int(end_time.timestamp() * 1000),
            'limit': limit
        }
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if not data:
                break
            
            for k in data:
                kline = {
                    'timestamp': datetime.fromtimestamp(k[0] / 1000).isoformat(),
                    'open': str(k[1]),
                    'high': str(k[2]),
                    'low': str(k[3]),
                    'close': str(k[4]),
                    'volume': str(k[5]),
                    'quote_volume': str(k[7]),
                    'trades_count': k[8]
                }
                all_klines.append(kline)
            
            if len(data) < limit:
                break
            
            last_time = datetime.fromtimestamp(data[-1][0] / 1000)
            current_start = last_time + timedelta(minutes=1)
            
            time.sleep(0.1)
            
        except requests.exceptions.RequestException as e:
            print(f"获取 {symbol} {interval} 数据失败：{e}")
            time.sleep(1)
            continue
    
    print(f"{symbol} {interval}: 获取 {len(all_klines)} 条 K 线")
    return all_klines


def fetch_multi_timeframe_data(symbols: List[str], days: int = 180):
    """获取多时间框架数据（日线、4 小时、1 小时）"""
    end_time = datetime.now()
    start_time = end_time - timedelta(days=days)
    
    print("=" * 80)
    print("开始获取多时间框架历史数据")
    print(f"币种：{', '.join(symbols)}")
    print(f"时间范围：{start_time} ~ {end_time}")
    print(f"时间框架：日线 (1d)、4 小时 (4h)、1 小时 (1h)")
    print("=" * 80)
    
    multi_timeframe_data = {}
    
    for symbol in symbols:
        print(f"\n正在获取 {symbol} 数据...")
        
        print(f"  获取日线 (1d)...")
        daily_klines = fetch_binance_klines(symbol, '1d', start_time, end_time, 1000)
        
        print(f"  获取 4 小时 (4h)...")
        k4h_klines = fetch_binance_klines(symbol, '4h', start_time, end_time, 1000)
        
        print(f"  获取 1 小时 (1h)...")
        k1h_klines = fetch_binance_klines(symbol, '1h', start_time, end_time, 1000)
        
        multi_timeframe_data[symbol] = {
            '1d': daily_klines,
            '4h': k4h_klines,
            '1h': k1h_klines
        }
        
        print(f"✅ {symbol} 完成：日线{len(daily_klines)}条，4 小时{len(k4h_klines)}条，1 小时{len(k1h_klines)}条")
    
    total_klines = sum(len(klines) for data in multi_timeframe_data.values() for klines in data.values())
    
    print("\n" + "=" * 80)
    print("多时间框架数据获取完成")
    print(f"总 K 线数：{total_klines}")
    print("=" * 80)
    
    return multi_timeframe_data


if __name__ == '__main__':
    symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
    days = 180
    output_file = '/Users/yl/vscode/bianace_btcethbnb_trade/data/multi_timeframe_data.json'
    
    data = fetch_multi_timeframe_data(symbols, days)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"\n数据已保存到：{output_file}")
