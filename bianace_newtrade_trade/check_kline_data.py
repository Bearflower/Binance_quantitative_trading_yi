#!/usr/bin/env python3
"""检查K线数据"""

import json

data = json.load(open("data/2025_new_coins_data.json"))

symbols_data = data.get('data', {})
print(f"币种数量: {len(symbols_data)}")

for symbol in list(symbols_data.keys())[:5]:
    symbol_data = symbols_data[symbol]
    print(f"\n{symbol}:")
    print(f"  数据键: {list(symbol_data.keys())}")

    for interval in ['1h', '4h', '1d']:
        if interval in symbol_data:
            klines = symbol_data[interval]
            print(f"  {interval} K线数量: {len(klines)}")
            if klines:
                print(f"  {interval} 第一根K线: {klines[0]}")
                print(f"  {interval} 最后一根K线: {klines[-1]}")
