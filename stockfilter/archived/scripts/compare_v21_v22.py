#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对比 V2.1 和 V2.2 回测结果
"""

import json
from collections import defaultdict

# 加载 V2.2 结果
with open('backtest_results/backtest_v22_20260413_223608.json', 'r') as f:
    v22_data = json.load(f)

# 加载 V2.1 结果（baostocks_full 数据）
# 需要先运行 V2.1 回测获取完整数据
print("="*80)
print("V2.1 vs V2.2 回测对比分析")
print("="*80)

# V2.2 数据
v22_patterns = v22_data['patterns']
v22_trades = v22_data['trades']
v22_summary = v22_data['summary']

print("\n【V2.2 回测结果】")
print(f"  检测股票数：{v22_summary['total_stocks']}")
print(f"  满足形态数：{v22_summary['matched_stocks']}")
print(f"  总形态数：{v22_summary['total_patterns']}")
print(f"  总交易数：{v22_summary['total_trades']}")
print(f"  平均收益：{v22_summary['avg_return']:.2f}%")
print(f"  胜率：{v22_summary['win_rate']:.2f}%")
print(f"  最高收益：{v22_summary['max_return']:.2f}%")
print(f"  最低收益：{v22_summary['min_return']:.2f}%")

# 按年份统计 V2.2
v22_yearly = defaultdict(int)
for p in v22_patterns:
    year = p['retrace_date'][:4]
    v22_yearly[year] += 1

print("\n  年度分布：")
for year in sorted(v22_yearly.keys()):
    print(f"    {year}年：{v22_yearly[year]}个形态")

# V2.1 数据（从之前运行的结果）
# 基于本地 16 只股票的数据
v21_local_summary = {
    'total_stocks': 16,
    'matched_stocks': 5,
    'total_patterns': 5,
    'total_trades': 5,
    'avg_return': 0.35,
    'win_rate': 40.00,
    'max_return': 10.46,
    'min_return': -8.35
}

print("\n【V2.1 回测结果（本地 16 只股票）】")
print(f"  检测股票数：{v21_local_summary['total_stocks']}")
print(f"  满足形态数：{v21_local_summary['matched_stocks']}")
print(f"  总形态数：{v21_local_summary['total_patterns']}")
print(f"  总交易数：{v21_local_summary['total_trades']}")
print(f"  平均收益：{v21_local_summary['avg_return']:.2f}%")
print(f"  胜率：{v21_local_summary['win_rate']:.2f}%")
print(f"  最高收益：{v21_local_summary['max_return']:.2f}%")
print(f"  最低收益：{v21_local_summary['min_return']:.2f}%")

# 核心差异对比
print("\n" + "="*80)
print("核心差异对比")
print("="*80)

print("\n【时间窗口设置】")
print("  V2.1: 缩量→放量 = 10 天")
print("  V2.2: 缩量→放量 = 25 天 ⬆️")

print("\n【检测范围】")
print(f"  V2.1: {v21_local_summary['total_stocks']}只股票（本地数据）")
print(f"  V2.2: {v22_summary['total_stocks']}只股票（baostocks_full 完整数据）⬆️")

print("\n【形态数量】")
print(f"  V2.1: {v21_local_summary['total_patterns']}个")
print(f"  V2.2: {v22_summary['total_patterns']}个")
print(f"  增长率：{(v22_summary['total_patterns'] / v21_local_summary['total_patterns'] - 1) * 100:.0f}%")

print("\n【收益表现】")
print(f"  V2.1 平均收益：{v21_local_summary['avg_return']:.2f}%")
print(f"  V2.2 平均收益：{v22_summary['avg_return']:.2f}%")
print(f"  提升：{v22_summary['avg_return'] - v21_local_summary['avg_return']:.2f}个百分点")

print(f"\n  V2.1 胜率：{v21_local_summary['win_rate']:.2f}%")
print(f"  V2.2 胜率：{v22_summary['win_rate']:.2f}%")
print(f"  提升：{v22_summary['win_rate'] - v21_local_summary['win_rate']:.2f}个百分点")

# 检测到的股票对比
v22_codes = set(p['code'] for p in v22_patterns)
print(f"\n【覆盖股票】")
print(f"  V2.2 检测到形态的股票：{len(v22_codes)}只")
print(f"  占检测总数的：{len(v22_codes) / v22_summary['total_stocks'] * 100:.2f}%")

# 显示一些 V2.2 特有的形态
print("\n" + "="*80)
print("V2.2 形态示例（前 20 个）")
print("="*80)

for i, p in enumerate(v22_patterns[:20], 1):
    print(f"{i:2d}. {p['code']}: {p['retrace_date']} | 收益：{p.get('scheme', 'N/A')}")

# 603529 检测情况
print("\n" + "="*80)
print("603529 爱玛科技检测情况")
print("="*80)

v22_603529 = [p for p in v22_patterns if p['code'] == '603529']
if v22_603529:
    print(f"  ✅ V2.2 检测到 {len(v22_603529)} 个形态")
    for p in v22_603529:
        print(f"    - {p['retrace_date']}: {p['surge_date']} 放量")
else:
    print("  ❌ V2.2 未检测到 603529 的形态")
    print("  原因：603529 的缩量→放量时间跨度约 160 天，远超 25 天窗口")

print("\n" + "="*80)
print("总结")
print("="*80)
print("""
V2.2 相比 V2.1 的改进：
✅ 放宽缩量→放量时间窗口（10 天→25 天）
✅ 使用完整数据回测（16 只→3317 只股票）
✅ 检测到更多形态（5 个→478 个）
✅ 平均收益提升（0.35%→4.28%）
✅ 胜率提升（40%→64.96%）

❌ 603529 仍无法检测（时间跨度过大）

建议：
- V2.2 的 25 天窗口是合理平衡点
- 如需检测 603529，需进一步放宽到 160 天（不推荐）
- 可将 603529 加入手动观察列表
""")
