#!/usr/bin/env python3
"""
对比 V2.4 和 V2.5 的回测结果
"""

import json
from pathlib import Path
from collections import defaultdict

def compare_v24_v25():
    print("=" * 100)
    print("📊 V2.4 vs V2.5 回测结果对比")
    print("=" * 100)
    
    # 查找最新的回测结果文件
    v24_files = list(Path("backtest_results").glob("backtest_v24_full_*.json"))
    v25_files = list(Path("backtest_results").glob("backtest_v25_full_*.json"))
    
    if not v24_files:
        print("❌ 未找到 V2.4 回测结果")
        return
    
    if not v25_files:
        print("❌ 未找到 V2.5 回测结果")
        return
    
    v24_file = sorted(v24_files)[-1]
    v25_file = sorted(v25_files)[-1]
    
    print(f"\nV2.4 文件：{v24_file.name}")
    print(f"V2.5 文件：{v25_file.name}")
    
    # 加载 V2.4 结果
    with open(v24_file, 'r', encoding='utf-8') as f:
        v24_data = json.load(f)
    
    # 加载 V2.5 结果
    with open(v25_file, 'r', encoding='utf-8') as f:
        v25_data = json.load(f)
    
    # 提取汇总信息
    v24_patterns = v24_data.get('patterns', [])
    v24_trades = v24_data.get('trades', [])
    
    v25_patterns = v25_data.get('patterns', [])
    v25_trades = v25_data.get('trades', [])
    
    print("\n" + "=" * 100)
    print("📊 核心指标对比")
    print("=" * 100)
    
    print(f"\n{'指标':<20} {'V2.4(97%)':<15} {'V2.5(94%)':<15} {'差异':<15}")
    print("-" * 65)
    
    # 信号数量
    v24_count = len(v24_patterns)
    v25_count = len(v25_patterns)
    print(f"{'信号数量':<20} {v24_count:<15} {v25_count:<15} {v25_count-v24_count:+d} ({(v25_count/v24_count-1)*100:+.1f}%)")
    
    # 交易数量
    v24_trades_count = len(v24_trades)
    v25_trades_count = len(v25_trades)
    print(f"{'交易数量':<20} {v24_trades_count:<15} {v25_trades_count:<15} {v25_trades_count-v24_trades_count:+d} ({(v25_trades_count/v24_trades_count-1)*100:+.1f}%)")
    
    # 年度分布
    print("\n" + "=" * 100)
    print("📅 年度分布对比")
    print("=" * 100)
    
    v24_year_dist = defaultdict(int)
    for p in v24_patterns:
        year = int(p['surge_date'][:4])
        v24_year_dist[year] += 1
    
    v25_year_dist = defaultdict(int)
    for p in v25_patterns:
        year = int(p['surge_date'][:4])
        v25_year_dist[year] += 1
    
    all_years = sorted(set(list(v24_year_dist.keys()) + list(v25_year_dist.keys())))
    
    print(f"\n{'年份':<10} {'V2.4':<10} {'V2.5':<10} {'差异':<10}")
    print("-" * 40)
    
    for year in all_years:
        v24_val = v24_year_dist.get(year, 0)
        v25_val = v25_year_dist.get(year, 0)
        diff = v25_val - v24_val
        print(f"{year:<10} {v24_val:<10} {v25_val:<10} {diff:+d} ({(v25_val/v24_val-1)*100:+.1f}%)")
    
    # 排除 2019 年
    v24_non_2019 = sum(v for k, v in v24_year_dist.items() if k != 2019)
    v25_non_2019 = sum(v for k, v in v25_year_dist.items() if k != 2019)
    
    print(f"\n排除 2019 年：V2.4={v24_non_2019}, V2.5={v25_non_2019}, 差异={v25_non_2019-v24_non_2019:+d}")
    
    # 收益统计
    print("\n" + "=" * 100)
    print("💰 收益统计对比")
    print("=" * 100)
    
    if v24_trades and v25_trades:
        v24_profitable = sum(1 for t in v24_trades if t.get('is_profitable', False))
        v25_profitable = sum(1 for t in v25_trades if t.get('is_profitable', False))
        
        v24_total_return = sum(t.get('actual_return', 0) for t in v24_trades)
        v25_total_return = sum(t.get('actual_return', 0) for t in v25_trades)
        
        v24_avg_return = v24_total_return / len(v24_trades) if v24_trades else 0
        v25_avg_return = v25_total_return / len(v25_trades) if v25_trades else 0
        
        print(f"\n{'指标':<20} {'V2.4(97%)':<15} {'V2.5(94%)':<15} {'差异':<15}")
        print("-" * 65)
        
        print(f"{'盈利交易':<20} {v24_profitable}/{v24_trades_count:<10} {v25_profitable}/{v25_trades_count:<10} "
              f"{(v25_profitable-v24_profitable):+d}")
        
        v24_win_rate = v24_profitable / len(v24_trades) * 100 if v24_trades else 0
        v25_win_rate = v25_profitable / len(v25_trades) * 100 if v25_trades else 0
        
        print(f"{'胜率':<20} {v24_win_rate:>10.2f}% {v25_win_rate:>10.2f}% {(v25_win_rate-v24_win_rate):>+.2f}%")
        
        print(f"{'总收益':<20} {v24_total_return:>10.2%} {v25_total_return:>10.2%} "
              f"{(v25_total_return-v24_total_return):>+.2%}")
        
        print(f"{'平均收益':<20} {v24_avg_return:>10.2%} {v25_avg_return:>10.2%} "
              f"{(v25_avg_return-v24_avg_return):>+.2%}")
    
    # 结论
    print("\n" + "=" * 100)
    print("📊 结论")
    print("=" * 100)
    
    if v25_count > v24_count:
        print(f"\n✅ V2.5 放宽回踩要求后，信号数量增加 {v25_count-v24_count} 个 ({(v25_count/v24_count-1)*100:+.1f}%)")
    else:
        print(f"\n⚠️  V2.5 信号数量反而减少 {v24_count-v25_count} 个")
    
    if v25_trades_count > v24_trades_count:
        print(f"✅ V2.5 交易数量增加 {v25_trades_count-v24_trades_count} 笔")
    else:
        print(f"⚠️  V2.5 交易数量减少")
    
    if v25_total_return > v24_total_return:
        print(f"✅ V2.5 总收益更高 ({v24_total_return:.2%} → {v25_total_return:.2%})")
    else:
        print(f"⚠️  V2.5 总收益更低")
    
    print("\n" + "=" * 100)

if __name__ == "__main__":
    compare_v24_v25()
