#!/usr/bin/env python3
"""检查数据结构"""

import json

data = json.load(open("data/2025_new_coins_data.json"))

print(f"数据顶层键: {list(data.keys())}")
print(f"\n数据结构:")

for key in data.keys():
    print(f"\n{key}:")
    if isinstance(data[key], dict):
        print(f"  类型: dict")
        print(f"  键: {list(data[key].keys())[:10]}")
        if 'symbols' in data[key]:
            print(f"  symbols数量: {len(data[key]['symbols'])}")
            if data[key]['symbols']:
                first_symbol_data = data[key]['symbols'][0]
                print(f"  第一个symbol数据键: {list(first_symbol_data.keys())}")
    elif isinstance(data[key], list):
        print(f"  类型: list")
        print(f"  长度: {len(data[key])}")
        if data[key]:
            print(f"  第一个元素: {data[key][0] if not isinstance(data[key][0], dict) else list(data[key][0].keys())}")
    else:
        print(f"  类型: {type(data[key])}")
        print(f"  值: {data[key]}")
