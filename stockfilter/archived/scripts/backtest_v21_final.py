#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.1 最终版回测（优化版 - 解决 2019 年信号过多问题）

问题分析：
- 2019 年 285 个信号（占 89.6%），其中 228 个集中在 2019 年 2 月
- 2020-2025 年总共只有 33 个信号，年均 5.5 个
- 原因：2018 年大跌后，2019 年初大量超跌反弹，参数过拟合

优化方案：
1. 同一只股票两次信号至少间隔 60 个交易日
2. 每只股票每年最多 2 个信号
3. 增加信号分布合理性分析
"""

import pandas as pd
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict
from collections import Counter, defaultdict

from backtester_scheme_ab import BacktesterWithRules_AB
from utils.logger import get_logger

logger = get_logger()


def run_optimized_backtest():
    """运行优化后的回测（解决 2019 年信号过多问题）"""
    
    print("=" * 80)
    print("股票形态策略 V2.1 最终版回测（优化版）")
    print("优化目标：解决 2019 年信号过多问题，使信号分布更均衡")
    print("=" * 80)
    
    # 加载配置
    config_file = 'config_v21_final.yaml'
    if not Path(config_file).exists():
        print(f"❌ 配置文件不存在：{config_file}")
        return
    
    # 初始化回测器
    backtester = BacktesterWithRules_AB(config_path=config_file)
    
    # 数据目录
    data_dir = Path('data/backtest/baostocks_full')
    if not data_dir.exists():
        print("❌ 数据目录不存在")
        return
    
    # 获取所有 CSV 文件
    csv_files = list(data_dir.glob('*.csv'))
    total = len(csv_files)
    print(f"\n找到 {total} 只股票")
    print("=" * 80)
    
    # 存储所有形态信号
    all_signals = []
    
    # 跟踪每只股票的交易历史（用于间隔控制）
    stock_trade_history = defaultdict(list)  # {stock_code: [buy_date1, buy_date2, ...]}
    
    # 跟踪每年每只股票的信号数量（用于年度限制）
    stock_yearly_count = defaultdict(lambda: defaultdict(int))  # {stock_code: {year: count}}
    
    for idx, csv_file in enumerate(csv_files):
        # 文件名格式：000001_data.csv → code=000001
        code = csv_file.stem.split('_')[0]
        name = csv_file.stem  # 保留股票名称（包含市场后缀）
        
        print(f"[{idx+1}/{total}] {code} - 检测中...", end=" ")
        
        # 加载数据
        try:
            df = pd.read_csv(csv_file)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
        except Exception as e:
            print(f"❌ 数据加载失败：{e}")
            continue
        
        if len(df) < 60:
            print("❌ 数据不足")
            continue
        
        # 检测所有形态（不只是第一个）
        try:
            # 使用改进的检测方法，返回所有形态
            all_patterns = backtester.check_all_patterns(df, code, '2019-01-01', '2026-04-07')
            
            if all_patterns and len(all_patterns) > 0:
                print(f"✅ 发现 {len(all_patterns)} 个形态")
                
                # 对每个形态进行过滤和交易模拟
                for pattern_idx, pattern_info in enumerate(all_patterns):
                    # 获取回踩日期（买入日期）
                    retrace_date = pd.to_datetime(pattern_info.get('retrace_date'))
                    buy_year = retrace_date.year
                    
                    # 过滤规则 1：检查年度信号数量限制（每年最多 2 个）
                    if stock_yearly_count[code][buy_year] >= 2:
                        continue  # 超过年度限制，跳过
                    
                    # 过滤规则 2：检查信号间隔（至少 60 个交易日）
                    if stock_trade_history[code]:
                        last_buy_date = stock_trade_history[code][-1]
                        # 计算交易日间隔
                        trading_days_between = len(df[(df['date'] > last_buy_date) & (df['date'] <= retrace_date)])
                        if trading_days_between < 60:
                            continue  # 间隔不足 60 天，跳过
                    
                    # 模拟交易
                    trade_result = backtester.simulate_trade(df, pattern_info)
                    
                    if trade_result:
                        signal = {
                            'stock_code': code,
                            'stock_name': name,
                            'pattern_index': pattern_idx + 1,  # 第几个形态
                            # 交易数据
                            'buy_date': str(trade_result.get('buy_date', '')),
                            'sell_date': str(trade_result.get('sell_date', '')),
                            'buy_price': trade_result.get('buy_price', 0),
                            'sell_price': trade_result.get('sell_price', 0),
                            'profit_pct': trade_result.get('net_return', 0) * 100,
                            'hold_days': trade_result.get('holding_days', 0),
                            'exit_reason': trade_result.get('sell_reason', ''),
                            # 形态数据
                            'retrace_date': str(pattern_info.get('retrace_date', '')),
                            'surge_date': str(pattern_info.get('surge_date', '')),
                            'drop_start_date': str(pattern_info.get('drop_start_date', '')),
                            'drop_end_date': str(pattern_info.get('drop_end_date', '')),
                            'shrink_date': str(pattern_info.get('shrink_date', '')),
                            'support_level': pattern_info.get('support_level', 0),
                        }
                        all_signals.append(signal)
                        
                        # 更新跟踪记录
                        buy_date = pd.to_datetime(trade_result.get('buy_date'))
                        stock_trade_history[code].append(buy_date)
                        stock_yearly_count[code][buy_year] += 1
            else:
                print("❌ 不满足")
        except Exception as e:
            print(f"❌ 异常：{e}")
    
    print()
    print("=" * 80)
    print(f"回测完成：共检测 {total} 只股票，发现 {len(all_signals)} 个形态信号（经过滤）")
    print("=" * 80)
    
    # 统计
    if all_signals:
        profits = [s['profit_pct'] for s in all_signals]
        avg_profit = sum(profits) / len(profits)
        max_profit = max(profits)
        min_profit = min(profits)
        profitable = len([p for p in profits if p > 0])
        win_rate = profitable / len(all_signals) * 100
        
        print(f"\n📊 统计结果:")
        print(f"  总信号数：{len(all_signals)} 个")
        print(f"  平均收益：{avg_profit:.2f}%")
        print(f"  最高收益：{max_profit:.2f}%")
        print(f"  最低收益：{min_profit:.2f}%")
        print(f"  胜率：{win_rate:.1f}%")
        
        # 按年份统计
        print(f"\n📈 按买入年份统计:")
        for signal in all_signals:
            if signal['buy_date']:
                try:
                    buy_year = pd.to_datetime(signal['buy_date']).year
                    signal['buy_year'] = buy_year
                except:
                    signal['buy_year'] = None
        
        year_counts = Counter([s['buy_year'] for s in all_signals if s['buy_year']])
        years = sorted(year_counts.keys())
        
        # 计算年均信号（排除 2019 年，因为数据从 2019 年开始不完整）
        non_2019_years = [y for y in years if y != 2019]
        non_2019_signals = sum(year_counts[y] for y in non_2019_years)
        avg_per_year_excluding_2019 = non_2019_signals / len(non_2019_years) if non_2019_years else 0
        
        for year in years:
            year_signals = [s for s in all_signals if s['buy_year'] == year]
            year_profits = [s['profit_pct'] for s in year_signals]
            avg_year_profit = sum(year_profits) / len(year_profits) if year_profits else 0
            percentage = (year_counts[year] / len(all_signals) * 100)
            print(f"  {year}年：{year_counts[year]} 个信号 ({percentage:.1f}%)，平均收益 {avg_year_profit:.2f}%")
        
        print(f"\n  年均信号（2020-2025）：{avg_per_year_excluding_2019:.1f} 个/年")
        print(f"  年均信号（2019-2025）：{len(all_signals) / len(years):.1f} 个/年")
        
        # 信号分布合理性分析
        print(f"\n🔍 信号分布合理性分析:")
        
        # 1. 年度分布均匀度
        if len(years) > 1:
            max_year_count = max(year_counts[y] for y in years)
            min_year_count = min(year_counts[y] for y in years)
            distribution_ratio = max_year_count / min_year_count if min_year_count > 0 else float('inf')
            print(f"  年度分布：最多 {max_year_count} 个，最少 {min_year_count} 个，比率 {distribution_ratio:.2f}x")
            
            if distribution_ratio > 5:
                print(f"  ⚠️  警告：年度分布不均（比率>5x），信号集中在某些年份")
            else:
                print(f"  ✅ 年度分布较为均衡")
        
        # 2. 2019 年集中度
        if 2019 in year_counts:
            ratio_2019 = year_counts[2019] / len(all_signals) * 100
            if ratio_2019 > 50:
                print(f"  ⚠️  警告：2019 年信号占比过高 ({ratio_2019:.1f}%)")
            else:
                print(f"  ✅ 2019 年信号占比合理 ({ratio_2019:.1f}%)")
        
        # 3. 月度分布（针对 2019 年）
        if 2019 in year_counts and year_counts[2019] > 10:
            signals_2019 = [s for s in all_signals if s['buy_year'] == 2019]
            month_counts = Counter([pd.to_datetime(s['buy_date']).month for s in signals_2019])
            max_month = max(month_counts.values())
            print(f"  2019 年月度分布：最多单月 {max_month} 个信号")
            if max_month > year_counts[2019] * 0.5:
                print(f"  ⚠️  警告：2019 年信号过度集中在某个月")
            else:
                print(f"  ✅ 2019 年月度分布较为分散")
        
        # 保存结果
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = Path(f'backtest_results/backtest_v21_final_optimized_{timestamp}.json')
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(all_signals, f, ensure_ascii=False, indent=2, default=str)
        
        print(f"\n✅ 结果已保存：{output_file}")
        
        # 保存 CSV
        csv_file = Path(f'backtest_results/backtest_v21_final_optimized_{timestamp}.csv')
        df_signals = pd.DataFrame(all_signals)
        df_signals.to_csv(csv_file, index=False, encoding='utf-8-sig')
        print(f"✅ CSV 已保存：{csv_file}")
        
        # 生成对比报告
        print(f"\n" + "=" * 80)
        print("📋 优化前后对比:")
        print("=" * 80)
        print("优化前（原始回测）:")
        print("  - 总信号数：318 个")
        print("  - 2019 年：285 个 (89.6%)")
        print("  - 2020-2025 年：33 个 (10.4%)")
        print("  - 年均信号（2019-2025）：45.4 个/年")
        print("  - 问题：2019 年信号过多，不代表长期稳定表现")
        print()
        print("优化后（最终版）:")
        print(f"  - 总信号数：{len(all_signals)} 个")
        print(f"  - 2019 年：{year_counts.get(2019, 0)} 个 ({year_counts.get(2019, 0) / len(all_signals) * 100:.1f}%)")
        print(f"  - 2020-2025 年：{non_2019_signals} 个 ({non_2019_signals / len(all_signals) * 100:.1f}%)")
        print(f"  - 年均信号（2020-2025）：{avg_per_year_excluding_2019:.1f} 个/年")
        print(f"  - 年均信号（2019-2025）：{len(all_signals) / len(years):.1f} 个/年")
        print(f"  - 平均收益：{avg_profit:.2f}%")
        print(f"  - 胜率：{win_rate:.1f}%")
        print()
        print("优化措施:")
        print("  ✅ 同一只股票两次信号至少间隔 60 个交易日")
        print("  ✅ 每只股票每年最多 2 个信号")
        print("  ✅ 信号分布更均衡，避免单一年份过度集中")
        
    else:
        print("\n❌ 没有发现任何形态信号")
    
    return all_signals


if __name__ == '__main__':
    run_optimized_backtest()
