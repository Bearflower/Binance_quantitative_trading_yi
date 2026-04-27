#!/usr/bin/env python3
"""检查K线数据结构"""

import json

data = json.load(open("data/2025_new_coins_data.json"))

symbols_data = data.get('data', {})
print(f"币种数量: {len(symbols_data)}")

for symbol in list(symbols_data.keys())[:1]:
    symbol_data = symbols_data[symbol]
    print(f"\n{symbol}:")
    print(f"  数据键: {list(symbol_data.keys())}")

    for interval in ['1h', '4h', '1d']:
        if interval in symbol_data:
            klines = symbol_data[interval]
            print(f"\n  {interval} K线:")
            print(f"    类型: {type(klines)}")
            if isinstance(klines, dict):
                print(f"    键: {list(klines.keys())[:10]}")
                print(f"    长度: {len(klines)}")
                if klines:
                    first_key = list(klines.keys())[0]
                    print(f"    第一根K线键: {first_key}")
                    print(f"    第一根K线数据: {klines[first_key]}")
            elif isinstance(klines, list):
                print(f"    长度: {len(klines)}")
                if klines:
                    print(f"    第一根K线: {klines[0]}")
