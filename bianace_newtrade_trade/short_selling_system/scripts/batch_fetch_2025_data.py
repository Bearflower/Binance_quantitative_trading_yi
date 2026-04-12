#!/usr/bin/env python3
"""
批量获取 2025 年新币的历史数据
支持多时间框架（1d/4h/1h/15m/5m）
"""

import requests
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict


def fetch_klines(symbol: str, interval: str, limit: int = 1000) -> List[List]:
    """获取 K 线数据"""
    url = 'https://fapi.binance.com/fapi/v1/klines'
    
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': min(limit, 1000)
    }
    
    try:
        response = requests.get(url, params=params, timeout=30)
        data = response.json()
        
        if isinstance(data, list):
            return data
        else:
            print(f"  ❌ {symbol} {interval}: {data}")
            return []
    
    except Exception as e:
        print(f"  ❌ {symbol} {interval} 请求失败：{e}")
        return []


def format_kline_data(symbol: str, klines: List[List], interval: str) -> Dict:
    """格式化 K 线数据"""
    formatted = []
    
    for k in klines:
        formatted.append({
            'timestamp': k[0],
            'open': k[1],
            'high': k[2],
            'low': k[3],
            'close': k[4],
            'volume': k[5],
            'close_time': k[6],
            'quote_volume': k[7],
            'trades': k[8]
        })
    
    return {
        'symbol': symbol,
        'interval': interval,
        'data': formatted,
        'count': len(formatted)
    }


def batch_fetch_symbols(symbols: List[str], intervals: List[str], output_path: str):
    """批量获取多个币种的历史数据"""
    
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    
    all_data = {
        'metadata': {
            'fetch_time': datetime.now().isoformat(),
            'total_symbols': len(symbols),
            'intervals': intervals,
            'symbols': symbols
        },
        'data': {}
    }
    
    total = len(symbols) * len(intervals)
    current = 0
    
    print("=" * 80)
    print(f"批量获取历史数据")
    print("=" * 80)
    print(f"币种数量：{len(symbols)}")
    print(f"时间框架：{', '.join(intervals)}")
    print(f"总请求数：{total}")
    print("=" * 80)
    
    for symbol in symbols:
        print(f"\n正在处理：{symbol}")
        all_data['data'][symbol] = {}
        
        for interval in intervals:
            current += 1
            progress = f"{current}/{total} ({current/total*100:.1f}%)"
            
            print(f"  [{progress}] 获取 {interval} K 线...")
            
            # 获取 K 线
            klines = fetch_klines(symbol, interval, limit=1000)
            
            if klines:
                formatted = format_kline_data(symbol, klines, interval)
                all_data['data'][symbol][interval] = formatted
                print(f"    ✅ 获取到 {len(klines)} 条 {interval} K 线")
            else:
                print(f"    ❌ 获取失败")
                all_data['data'][symbol][interval] = {
                    'symbol': symbol,
                    'interval': interval,
                    'data': [],
                    'count': 0
                }
            
            # 避免 API 限制，每次请求间隔 0.1 秒
            time.sleep(0.1)
        
        # 每个币种之间间隔 1 秒
        time.sleep(1)
        
        # 每 50 个币种保存一次进度
        if len(all_data['data']) % 50 == 0:
            temp_path = output_path.replace('.json', '_temp.json')
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(all_data, f, ensure_ascii=False, indent=2)
            print(f"\n  💾 已保存进度到 {temp_path}")
    
    # 保存最终结果
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    
    print("\n" + "=" * 80)
    print(f"✅ 批量获取完成！")
    print(f"数据已保存到：{output}")
    print("=" * 80)
    
    # 统计
    total_klines = 0
    for symbol_data in all_data['data'].values():
        for interval_data in symbol_data.values():
            total_klines += interval_data.get('count', 0)
    
    print(f"总 K 线数量：{total_klines:,}")
    print(f"平均每个币种：{total_klines/len(symbols):.0f} 条")


def main():
    # 1. 加载 2025 年新币列表
    print("加载 2025 年新币列表...")
    with open('data/2025_new_coins.json', 'r', encoding='utf-8') as f:
        new_coins = json.load(f)
    
    print(f"✅ 找到 {len(new_coins)} 个新币")
    
    # 2. 提取符号列表
    symbols = [coin['symbol'] for coin in new_coins]
    
    # 3. 设置时间框架（添加 30m）
    intervals = ['1d', '4h', '1h', '30m', '15m', '5m']
    
    # 4. 批量获取数据
    batch_fetch_symbols(
        symbols=symbols,
        intervals=intervals,
        output_path='data/2025_new_coins_data.json'
    )


if __name__ == '__main__':
    main()
