#!/usr/bin/env python3
"""检查数据结构"""

import json

data = json.load(open("data/2025_new_coins_data.json"))

print(f"数据顶层键: {list(data.keys())}")
print(f"\nmetadata:")
print(f"  fetch_time: {data['metadata']['fetch_time']}")
print(f"  total_symbols: {data['metadata']['total_symbols']}")
print(f"  intervals: {data['metadata']['intervals']}")
print(f"  symbols (前10个): {data['metadata']['symbols'][:10]}")

print(f"\ndata:")
print(f"  类型: {type(data['data'])}")
if isinstance(data['data'], dict):
    print(f"  键数量: {len(data['data'])}")
    symbols = list(data['data'].keys())
    print(f"  前10个币种: {symbols[:10]}")

    if symbols:
        first_symbol = symbols[0]
        print(f"\n  示例币种 {first_symbol}:")
        print(f"    数据键: {list(data['data'][first_symbol].keys())}")

        if 'klines' in data['data'][first_symbol]:
            print(f"    K线键: {list(data['data'][first_symbol]['klines'].keys())}")

            for interval in ['1h', '4h', '1d']:
                if interval in data['data'][first_symbol]["klines"]:
                    kline_count = len(data['data'][first_symbol]['klines'][interval])
                    print(f"    {interval} K线数量: {kline_count}")
