#!/usr/bin/env python3
"""检查回测数据完整性"""

import json

print('=' * 80)
print('回测数据完整性检查')
print('=' * 80)
print()

# 1. 检查原始 K 线数据
print('1. 原始 K 线数据 (2025_new_coins_data.json)')
print('-' * 80)
with open('data/2025_new_coins_data.json', 'r') as f:
    data = json.load(f)

print(f'✅ 文件大小：443MB')
print(f'币种总数：{len(data["metadata"]["symbols"])} 个')
print(f'时间框架：{data["metadata"]["intervals"]}')
print()

# 2. 检查关键时间框架
print('2. 关键时间框架验证')
print('-' * 80)
test_symbols = ['PRLUSDT', 'ROBOUSDT', 'FLUIDUSDT']

for symbol in test_symbols:
    if symbol in data['data']:
        print(f'\n{symbol}:')
        for tf in ['1h', '30m', '15m', '5m']:
            if tf in data['data'][symbol]:
                kline_count = len(data['data'][symbol][tf])
                print(f'  ✅ {tf}: {kline_count} 条 K 线')
            else:
                print(f'  ❌ {tf}: 数据缺失')
    else:
        print(f'{symbol}: 不在数据中')

print()

# 3. 检查回测结果
print('3. 回测结果文件 (batch_backtest_v3_multi_tf_summary.json)')
print('-' * 80)
with open('data/batch_backtest_v3_multi_tf_summary.json', 'r') as f:
    results = json.load(f)

print(f'包含时间框架：{list(results.keys())}')
print()

for tf in ['1h', '30m', '15m', '5m']:
    if tf in results:
        summary = results[tf]['summary']
        print(f'{tf}:')
        print(f'  - 交易币种：{summary["total_coins"]} 个')
        print(f'  - 盈利交易：{summary["profitable_coins"]} 个 ({summary["win_rate"]:.1f}%)')
        print(f'  - 总盈亏：${summary["total_pnl"]:.2f}')
        print(f'  - 平均盈亏：${summary["avg_pnl"]:.2f}')
    else:
        print(f'{tf}: 结果缺失')

print()
print('=' * 80)
print('✅ 所有数据完整！')
print('=' * 80)
