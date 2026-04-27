#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析 V2.4 回测结果：
1. 检查 2024 年的 11 个信号
2. 检查 603529 是否被检测到
3. 对信号进行质量排序
"""

import json
import pandas as pd

# 加载 V2.4 全量结果
with open('backtest_results/backtest_v24_full_20260414_100828.json', 'r') as f:
    v24_data = json.load(f)

print("="*80)
print("V2.4 回测结果深度分析")
print("="*80)

# 1. 检查 2024 年的 11 个信号
print("\n" + "="*80)
print("1. 2024 年信号检查（共 11 个）")
print("="*80)

patterns_2024 = [p for p in v24_data['patterns'] if str(p['retrace_date'])[:4] == '2024']
print(f"\n检测到 {len(patterns_2024)} 个 2024 年信号：\n")

for i, p in enumerate(patterns_2024, 1):
    print(f"{i}. {p['code']}: {p['retrace_date']}")
    print(f"   下跌：{p['drop_start_date']} 到 {p['drop_end_date']} ({p['drop_change']*100:.1f}%)")
    print(f"   放量：{p['surge_date']} ({p['surge_close']})")
    print()

# 检查 2024 年数据完整性
print("\n数据完整性检查：")
print(f"2024 年信号数：{len(patterns_2024)} 个")
print(f"平均每月信号数：{len(patterns_2024)/12:.2f} 个/月")

# 2. 检查 603529 是否被检测到
print("\n" + "="*80)
print("2. 603529 爱玛科技检测情况")
print("="*80)

patterns_603529 = [p for p in v24_data['patterns'] if p['code'] == '603529']

if patterns_603529:
    print(f"\n✅ V2.4 检测到 {len(patterns_603529)} 个形态：")
    for i, p in enumerate(patterns_603529, 1):
        print(f"\n形态 {i}:")
        print(f"  回踩日期：{p['retrace_date']}")
        print(f"  放量日期：{p['surge_date']}")
        print(f"  放量收盘价：{p['surge_close']}")
        print(f"  下跌区间：{p['drop_start_date']} 到 {p['drop_end_date']}")
        print(f"  跌幅：{p['drop_change']*100:.2f}%")
        print(f"  缩量日期：{p['shrink_date']}")
        print(f"  缩量到放量天数：{p['shrink_to_surge_days']}")
        
        # 计算实际天数
        from datetime import datetime
        shrink_dt = pd.to_datetime(p['shrink_date'])
        surge_dt = pd.to_datetime(p['surge_date'])
        actual_days = (surge_dt - shrink_dt).days
        print(f"  实际天数：{actual_days} 天")
else:
    print("\n❌ V2.4 未检测到 603529 的形态")
    print("原因分析：")
    print("  1. 缩量→放量时间跨度可能超过 60 天")
    print("  2. 2025 年 8-9 月可能没有符合其他条件的形态")
    print("  3. 流动性可能不满足 2000 万要求")

# 3. 信号质量排序
print("\n" + "="*80)
print("3. 信号质量排序（排除 2019 年）")
print("="*80)

# 排除 2019 年的信号
patterns_exclude_2019 = [p for p in v24_data['patterns'] if str(p['retrace_date'])[:4] != '2019']

# 计算质量分数
def calculate_quality_score(pattern):
    """
    质量评分标准：
    1. 跌幅越大越好（8-20 分）
    2. 缩量到放量时间越短越好（10-40 分）
    3. 放量涨幅适中（3-8% 最佳，10-20 分）
    4. 量比适中（1.5-5 倍最佳，10-20 分）
    5. 回踩确认越快越好（1-5 天最佳，10-20 分）
    """
    score = 0
    
    # 1. 跌幅评分（8-20 分）
    drop = pattern['drop_change']
    if 0.12 <= drop <= 0.20:
        drop_score = 20
    elif 0.08 <= drop < 0.12:
        drop_score = 15
    elif drop > 0.20:
        drop_score = 10
    else:
        drop_score = 5
    score += drop_score
    
    # 2. 缩量到放量时间评分（10-40 分）
    shrink_dt = pd.to_datetime(pattern['shrink_date'])
    surge_dt = pd.to_datetime(pattern['surge_date'])
    days = (surge_dt - shrink_dt).days
    
    if 5 <= days <= 15:
        time_score = 40
    elif 15 < days <= 25:
        time_score = 30
    elif 25 < days <= 40:
        time_score = 20
    else:
        time_score = 10
    score += time_score
    
    # 3. 回踩确认速度（10-20 分）
    retrace_dt = pd.to_datetime(pattern['retrace_date'])
    retrace_days = (retrace_dt - surge_dt).days
    
    if 1 <= retrace_days <= 3:
        retrace_score = 20
    elif 3 < retrace_days <= 5:
        retrace_score = 15
    else:
        retrace_score = 10
    score += retrace_score
    
    return score, drop_score, time_score, retrace_score

# 计算所有信号的质量分数
scored_patterns = []
for p in patterns_exclude_2019:
    total_score, drop_s, time_s, retrace_s = calculate_quality_score(p)
    scored_patterns.append({
        'pattern': p,
        'total_score': total_score,
        'drop_score': drop_s,
        'time_score': time_s,
        'retrace_score': retrace_s
    })

# 按质量分数排序
scored_patterns.sort(key=lambda x: x['total_score'], reverse=True)

print(f"\n共 {len(scored_patterns)} 个信号（排除 2019 年）")
print(f"平均质量分数：{sum(s['total_score'] for s in scored_patterns)/len(scored_patterns):.1f} 分")
print(f"最高分：{max(s['total_score'] for s in scored_patterns)} 分")
print(f"最低分：{min(s['total_score'] for s in scored_patterns)} 分")

print("\n" + "-"*80)
print("高质量信号 TOP 20（排除 2019 年）：")
print("-"*80)

for i, sp in enumerate(scored_patterns[:20], 1):
    p = sp['pattern']
    print(f"\n{i}. {p['code']} - 总分：{sp['total_score']} (跌幅:{sp['drop_score']} 时间:{sp['time_score']} 回踩:{sp['retrace_score']})")
    print(f"   回踩：{p['retrace_date']} | 放量：{p['surge_date']} | 收盘价：{p['surge_close']}")
    print(f"   跌幅：{p['drop_change']*100:.1f}% | 时间：{(pd.to_datetime(p['surge_date'])-pd.to_datetime(p['shrink_date'])).days}天")

# 按年份统计高质量信号
print("\n" + "="*80)
print("按年份统计高质量信号（总分≥60 分）")
print("="*80)

high_quality_by_year = {}
for sp in scored_patterns:
    if sp['total_score'] >= 60:
        year = str(sp['pattern']['retrace_date'])[:4]
        high_quality_by_year[year] = high_quality_by_year.get(year, 0) + 1

for year in sorted(high_quality_by_year.keys()):
    total_in_year = len([p for p in patterns_exclude_2019 if str(p['retrace_date'])[:4] == year])
    hq_count = high_quality_by_year[year]
    print(f"{year}年：{hq_count}个高质量信号（总{total_in_year}个，占比{hq_count/total_in_year*100:.1f}%）")

# 保存高质量信号列表
output_df = pd.DataFrame([
    {
        '代码': sp['pattern']['code'],
        '回踩日期': str(sp['pattern']['retrace_date']),
        '放量日期': str(sp['pattern']['surge_date']),
        '放量收盘价': sp['pattern']['surge_close'],
        '质量总分': sp['total_score'],
        '跌幅评分': sp['drop_score'],
        '时间评分': sp['time_score'],
        '回踩评分': sp['retrace_score'],
        '跌幅': sp['pattern']['drop_change']*100,
        '缩量到放量天数': (pd.to_datetime(sp['pattern']['surge_date'])-pd.to_datetime(sp['pattern']['shrink_date'])).days
    }
    for sp in scored_patterns[:100]
])

output_df.to_csv('backtest_results/v24_top100_quality_signals.csv', index=False, encoding='utf-8-sig')
print(f"\n前 100 个高质量信号已保存到：backtest_results/v24_top100_quality_signals.csv")

print("\n" + "="*80)
print("分析完成！")
print("="*80)
