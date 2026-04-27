#!/usr/bin/env python3
"""检查数据结构"""

import json

data = json.load(open("data/2025_new_coins_data.json"))
symbols = list(data.keys())
print(f"币种数量: {len(symbols)}")

if symbols:
    first_symbol = symbols[0]
    print(f"\n示例币种: {first_symbol}")
    print(f"数据键: {list(data[first_symbol].keys())}")

    if "klines" in data[first_symbol]:
        print(f"K线键: {list(data[first_symbol]['klines'].keys())}")

        for interval in ['1h', '4h', '1d']:
            if interval in data[first_symbol]["klines"]:
                print(f"{interval} K线数量: {len(data[first_symbol]['klines'][interval])}")
