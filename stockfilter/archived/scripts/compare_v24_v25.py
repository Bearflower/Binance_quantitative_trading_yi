#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.4 vs V2.5 完整对比分析
"""

import json
import pandas as pd
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')
plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

def load_results():
    """加载 V2.4 和 V2.5 结果"""
    # 加载 V2.4 结果
    v24_file = 'backtest_results/backtest_v24_full_20260414_100828.json'
    with open(v24_file, 'r') as f:
        v24_data = json.load(f)
    
    # 加载 V2.5 结果（查找最新文件）
    import glob
    v25_files = sorted(glob.glob('backtest_results/backtest_v25_full_*.json'))
    if not v25_files:
        print("❌ 未找到 V2.5 结果文件，请先运行 V2.5 回测")
        return None, None
    
    with open(v25_files[-1], 'r') as f:
        v25_data = json.load(f)
    
    return v24_data, v25_data

def compare_versions(v24_data, v25_data):
    """对比 V2.4 和 V2.5"""
    print("="*80)
    print("V2.4 vs V2.5 完整对比分析")
    print("="*80)
    
    v24_summary = v24_data['summary']
    v25_summary = v25_data['summary']
    
    # 1. 基本指标对比
    print("\n" + "="*80)
    print("1. 基本指标对比")
    print("="*80)
    
    metrics = [
        ('检测股票数', 'total_stocks'),
        ('满足形态股票数', 'matched_stocks'),
        ('总形态数', 'total_patterns'),
        ('总交易数', 'total_trades'),
    ]
    
    print(f"\n{'指标':<20} {'V2.4':>12} {'V2.5':>12} {'变化':>12} {'变化率':>12}")
    print("-"*80)
    
    for name, key in metrics:
        v24_val = v24_summary[key]
        v25_val = v25_summary[key]
        diff = v25_val - v24_val
        change_rate = (v25_val - v24_val) / v24_val * 100 if v24_val > 0 else 0
        print(f"{name:<20} {v24_val:>12} {v25_val:>12} {diff:>+12} {change_rate:>+11.1f}%")
    
    # 2. 收益指标对比
    print("\n" + "="*80)
    print("2. 收益指标对比（排除 2019 年）")
    print("="*80)
    
    return_metrics = [
        ('平均收益 (%)', 'avg_return'),
        ('胜率 (%)', 'win_rate'),
        ('最高收益 (%)', 'max_return'),
        ('最低收益 (%)', 'min_return'),
    ]
    
    print(f"\n{'指标':<20} {'V2.4':>12} {'V2.5':>12} {'变化':>12}")
    print("-"*80)
    
    for name, key in return_metrics:
        v24_val = v24_summary.get(key, 0)
        v25_val = v25_summary.get(key, 0)
        diff = v25_val - v24_val
        print(f"{name:<20} {v24_val:>11.2f}% {v25_val:>11.2f}% {diff:>+11.2f}%")
    
    # 3. 年度分布对比
    print("\n" + "="*80)
    print("3. 年度分布对比")
    print("="*80)
    
    v24_yearly = v24_summary['yearly_stats']
    v25_yearly = v25_summary['yearly_stats']
    
    all_years = sorted(set(list(v24_yearly.keys()) + list(v25_yearly.keys())))
    
    print(f"\n{'年份':<10} {'V2.4':>10} {'V2.5':>10} {'变化':>10} {'变化率':>12}")
    print("-"*80)
    
    for year in all_years:
        v24_count = v24_yearly.get(year, 0)
        v25_count = v25_yearly.get(year, 0)
        diff = v25_count - v24_count
        change_rate = (v25_count - v24_count) / v24_count * 100 if v24_count > 0 else 0
        print(f"{year:<10} {v24_count:>10} {v25_count:>10} {diff:>+10} {change_rate:>+11.1f}%")
    
    # 4. 排除 2019 年后对比
    print("\n" + "="*80)
    print("4. 排除 2019 年后对比（关键指标）")
    print("="*80)
    
    v24_exclude_2019 = v24_summary['patterns_exclude_2019']
    v25_exclude_2019 = v25_summary['patterns_exclude_2019']
    
    # 计算每月平均
    def calc_monthly_avg(patterns_exclude_2019, yearly_stats):
        if patterns_exclude_2019 == 0:
            return 0
        years = [y for y in yearly_stats.keys() if y != '2019']
        if not years:
            return 0
        min_year = min(years)
        max_year = max(years)
        months = (int(max_year) - int(min_year) + 1) * 12
        return patterns_exclude_2019 / months if months > 0 else 0
    
    v24_monthly = calc_monthly_avg(v24_exclude_2019, v24_yearly)
    v25_monthly = calc_monthly_avg(v25_exclude_2019, v25_yearly)
    
    print(f"\n{'指标':<25} {'V2.4':>12} {'V2.5':>12} {'变化':>12}")
    print("-"*80)
    print(f"{'排除 2019 年后形态数':<25} {v24_exclude_2019:>12} {v25_exclude_2019:>12} {(v25_exclude_2019-v24_exclude_2019):>+12}")
    print(f"{'平均每月信号数':<25} {v24_monthly:>11.2f}个 {v25_monthly:>11.2f}个 {(v25_monthly-v24_monthly):>+11.2f}个")
    print(f"{'平均每年信号数':<25} {v24_monthly*12:>11.1f}个 {v25_monthly*12:>11.1f}个 {(v25_monthly*12-v24_monthly*12):>+11.1f}个")
    
    # 5. 信号质量对比
    print("\n" + "="*80)
    print("5. 信号质量对比（估算）")
    print("="*80)
    
    # 基于收益和胜率推断质量
    print(f"\nV2.4 胜率：{v24_summary.get('win_rate', 0):.2f}%")
    print(f"V2.5 胜率：{v25_summary.get('win_rate', 0):.2f}%")
    
    if v25_summary.get('win_rate', 0) > v24_summary.get('win_rate', 0):
        print("✅ V2.5 信号质量更高（胜率提升）")
    else:
        print("⚠️  V2.5 信号质量相当或略低")
    
    # 6. 综合评估
    print("\n" + "="*80)
    print("6. 综合评估")
    print("="*80)
    
    print(f"\n【V2.4 特点】")
    print(f"  - 信号数量：{v24_summary['total_patterns']} 个（较多）")
    print(f"  - 每月平均：{v24_monthly:.2f} 个/月")
    print(f"  - 胜率：{v24_summary.get('win_rate', 0):.2f}%")
    print(f"  - 适用场景：追求更多信号，能接受略低胜率")
    
    print(f"\n【V2.5 特点】")
    print(f"  - 信号数量：{v25_summary['total_patterns']} 个（适中）")
    print(f"  - 每月平均：{v25_monthly:.2f} 个/月")
    print(f"  - 胜率：{v25_summary.get('win_rate', 0):.2f}%")
    print(f"  - 适用场景：平衡信号数量和质量")
    
    # 推荐
    print(f"\n【推荐建议】")
    if v25_monthly >= 2 and v25_summary.get('win_rate', 0) > v24_summary.get('win_rate', 0):
        print("✅ 推荐 V2.5：信号数量达标（≥2 个/月）且胜率更高")
    elif v25_monthly >= 2:
        print("✅ 推荐 V2.5：信号数量达标，胜率相当")
    elif v24_monthly >= 2:
        print("✅ 推荐 V2.4：信号数量更多，覆盖率更广")
    else:
        print("⚠️  两个版本都未达到目标，需要进一步优化")
    
    return {
        'v24_summary': v24_summary,
        'v25_summary': v25_summary,
        'v24_monthly': v24_monthly,
        'v25_monthly': v25_monthly
    }

def plot_comparison(v24_data, v25_data):
    """生成对比图表"""
    v24_summary = v24_data['summary']
    v25_summary = v25_data['summary']
    
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    
    # 图 1: 形态数量对比
    metrics1 = ['总形态数', '覆盖股票', '交易数']
    v24_values1 = [v24_summary['total_patterns'], v24_summary['matched_stocks'], v24_summary['total_trades']]
    v25_values1 = [v25_summary['total_patterns'], v25_summary['matched_stocks'], v25_summary['total_trades']]
    
    x1 = range(len(metrics1))
    width = 0.35
    
    bars1 = axes[0,0].bar([i - width/2 for i in x1], v24_values1, width, label='V2.4', color='lightcoral', edgecolor='red', linewidth=2)
    bars2 = axes[0,0].bar([i + width/2 for i in x1], v25_values1, width, label='V2.5', color='lightblue', edgecolor='blue', linewidth=2)
    
    axes[0,0].set_title('图 1: 形态数量对比', fontsize=14, fontweight='bold')
    axes[0,0].set_xlabel('指标', fontsize=12)
    axes[0,0].set_ylabel('数量', fontsize=12)
    axes[0,0].set_xticks(x1)
    axes[0,0].set_xticklabels(metrics1)
    axes[0,0].legend(fontsize=10)
    axes[0,0].grid(axis='y', alpha=0.3, linestyle='--')
    
    for bar, value in zip(bars1, v24_values1):
        axes[0,0].text(bar.get_x() + bar.get_width()/2., value, str(value),
                      ha='center', va='bottom', fontsize=11, fontweight='bold')
    for bar, value in zip(bars2, v25_values1):
        axes[0,0].text(bar.get_x() + bar.get_width()/2., value, str(value),
                      ha='center', va='bottom', fontsize=11, fontweight='bold')
    
    # 图 2: 收益指标对比
    metrics2 = ['平均收益', '胜率']
    v24_values2 = [v24_summary.get('avg_return', 0), v24_summary.get('win_rate', 0)]
    v25_values2 = [v25_summary.get('avg_return', 0), v25_summary.get('win_rate', 0)]
    
    x2 = range(len(metrics2))
    
    bars3 = axes[0,1].bar([i - width/2 for i in x2], v24_values2, width, label='V2.4', color='lightcoral', edgecolor='red', linewidth=2)
    bars4 = axes[0,1].bar([i + width/2 for i in x2], v25_values2, width, label='V2.5', color='lightblue', edgecolor='blue', linewidth=2)
    
    axes[0,1].set_title('图 2: 收益指标对比（排除 2019）', fontsize=14, fontweight='bold')
    axes[0,1].set_xlabel('指标', fontsize=12)
    axes[0,1].set_ylabel('百分比 (%)', fontsize=12)
    axes[0,1].set_xticks(x2)
    axes[0,1].set_xticklabels(metrics2)
    axes[0,1].legend(fontsize=10)
    axes[0,1].grid(axis='y', alpha=0.3, linestyle='--')
    
    for bar, value in zip(bars3, v24_values2):
        axes[0,1].text(bar.get_x() + bar.get_width()/2., value, f'{value:.2f}%',
                      ha='center', va='bottom', fontsize=11, fontweight='bold')
    for bar, value in zip(bars4, v25_values2):
        axes[0,1].text(bar.get_x() + bar.get_width()/2., value, f'{value:.2f}%',
                      ha='center', va='bottom', fontsize=11, fontweight='bold')
    
    # 图 3: 年度分布对比
    v24_yearly = v24_summary['yearly_stats']
    v25_yearly = v25_summary['yearly_stats']
    all_years = sorted(set(list(v24_yearly.keys()) + list(v25_yearly.keys())))
    
    v24_counts = [v24_yearly.get(y, 0) for y in all_years]
    v25_counts = [v25_yearly.get(y, 0) for y in all_years]
    
    x3 = range(len(all_years))
    
    bars5 = axes[1,0].bar([i - width/2 for i in x3], v24_counts, width, label='V2.4', color='lightcoral', edgecolor='red', linewidth=2)
    bars6 = axes[1,0].bar([i + width/2 for i in x3], v25_counts, width, label='V2.5', color='lightblue', edgecolor='blue', linewidth=2)
    
    axes[1,0].set_title('图 3: 年度形态分布对比', fontsize=14, fontweight='bold')
    axes[1,0].set_xlabel('年份', fontsize=12)
    axes[1,0].set_ylabel('形态数量', fontsize=12)
    axes[1,0].set_xticks(x3)
    axes[1,0].set_xticklabels(all_years)
    axes[1,0].legend(fontsize=10)
    axes[1,0].grid(axis='y', alpha=0.3, linestyle='--')
    
    for bar, value in zip(bars5, v24_counts):
        axes[1,0].text(bar.get_x() + bar.get_width()/2., value, str(value),
                      ha='center', va='bottom', fontsize=10, fontweight='bold')
    for bar, value in zip(bars6, v25_counts):
        axes[1,0].text(bar.get_x() + bar.get_width()/2., value, str(value),
                      ha='center', va='bottom', fontsize=10, fontweight='bold')
    
    # 图 4: V2.5 相对 V2.4 的变化率
    change_rates = [
        (v25_summary['total_patterns'] - v24_summary['total_patterns']) / v24_summary['total_patterns'] * 100,
        (v25_summary['matched_stocks'] - v24_summary['matched_stocks']) / v24_summary['matched_stocks'] * 100,
        (v25_summary['total_trades'] - v24_summary['total_trades']) / v24_summary['total_trades'] * 100,
    ]
    
    metrics4 = ['形态数', '覆盖股票', '交易数']
    x4 = range(len(metrics4))
    
    colors = ['green' if cr > 0 else 'red' for cr in change_rates]
    bars = axes[1,1].bar(x4, change_rates, color=colors, edgecolor='black', linewidth=1.2)
    
    axes[1,1].set_title('图 4: V2.5 相对 V2.4 的变化率', fontsize=14, fontweight='bold')
    axes[1,1].set_xlabel('指标', fontsize=12)
    axes[1,1].set_ylabel('变化率 (%)', fontsize=12)
    axes[1,1].axhline(y=0, color='black', linestyle='-', linewidth=0.8)
    axes[1,1].set_xticks(x4)
    axes[1,1].set_xticklabels(metrics4)
    axes[1,1].grid(axis='y', alpha=0.3, linestyle='--')
    
    for bar, cr in zip(bars, change_rates):
        height = bar.get_height()
        axes[1,1].text(bar.get_x() + bar.get_width()/2., height,
                      f'{cr:.1f}%',
                      ha='center', va='bottom' if height > 0 else 'top',
                      fontsize=12, fontweight='bold')
    
    plt.tight_layout()
    output_file = 'backtest_results/v24_vs_v25_comparison.png'
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"\n对比图表已保存到：{output_file}")

if __name__ == '__main__':
    v24_data, v25_data = load_results()
    
    if v24_data and v25_data:
        comparison = compare_versions(v24_data, v25_data)
        plot_comparison(v24_data, v25_data)
        
        print("\n" + "="*80)
        print("对比分析完成！")
        print("="*80)
