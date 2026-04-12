#!/usr/bin/env python3
"""
检查回测数据，找出有足够数据的币种
"""

import json
from pathlib import Path

def check_data_quality(data_path: str = 'data/backtest_data.json'):
    """检查数据质量"""
    
    with open(data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print("=" * 80)
    print("数据质量检查")
    print("=" * 80)
    
    total_symbols = len(data)
    valid_symbols = []
    
    for symbol, symbol_data in data.items():
        klines_1h = len(symbol_data.get('1h', []))
        klines_4h = len(symbol_data.get('4h', []))
        klines_1d = len(symbol_data.get('1d', []))
        funding_rates = len(symbol_data.get('funding_rate', []))
        
        # 判断是否有足够数据（至少 500 条 1 小时 K 线）
        has_enough_data = klines_1h >= 500
        
        if has_enough_data:
            valid_symbols.append(symbol)
        
        status = "✅" if has_enough_data else "⚠️ "
        
        print(f"\n{status} {symbol}")
        print(f"   1d: {klines_1d:4d} 条")
        print(f"   4h: {klines_4h:4d} 条")
        print(f"   1h: {klines_1h:4d} 条")
        print(f"   资金费率：{funding_rates:4d} 条")
        
        if klines_1h > 0:
            days = klines_1h / 24
            print(f"   时间跨度：约 {days:.1f} 天")
    
    print("\n" + "=" * 80)
    print(f"总币种数：{total_symbols}")
    print(f"数据充足（≥500 条 1h K 线）: {len(valid_symbols)} 个")
    if valid_symbols:
        print(f"可用币种：{', '.join(valid_symbols)}")
    else:
        print("⚠️  没有币种有足够数据，需要获取更长时间的历史数据")
    print("=" * 80)
    
    return valid_symbols


if __name__ == '__main__':
    valid_symbols = check_data_quality()
    
    if not valid_symbols:
        print("\n建议:")
        print("1. 在服务器上运行：python3 scripts/server_fetch_data.py --days 365")
        print("2. 或者更换其他上线时间更长的币种")
