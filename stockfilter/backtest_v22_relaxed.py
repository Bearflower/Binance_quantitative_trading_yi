#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.2 放宽参数版回测

放宽参数：
- 跌幅阈值：12% → 10%
- 量比要求：1.5 倍 → 1.2 倍
- 放量涨幅：5% → 4%
- 量比上限：12 倍 → 15 倍
"""

import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict
from collections import Counter

from backtester_v22 import BacktesterV22
from utils.logger import get_logger

logger = get_logger()


def run_v22_relaxed_backtest():
    """运行 V2.2 放宽参数版回测"""
    
    print("=" * 80)
    print("股票形态策略 V2.2 放宽参数版回测（2020-2025 年）")
    print("=" * 80)
    print()
    print("放宽参数:")
    print("  - 跌幅阈值：12% → 10%")
    print("  - 量比要求：1.5 倍 → 1.2 倍")
    print("  - 放量涨幅：5% → 4%")
    print("  - 量比上限：12 倍 → 15 倍")
    print("=" * 80)
    
    # 加载配置
    config_file = 'config_v22_relaxed.yaml'
    if not Path(config_file).exists():
        print(f"❌ 配置文件不存在：{config_file}")
        return
    
    # 初始化回测器
    backtester = BacktesterV22(config_path=config_file)
    
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
    
    for idx, csv_file in enumerate(csv_files):
        code = csv_file.stem.split('_')[0]
        name = csv_file.stem
        
        if idx % 100 == 0:
            print(f"[{idx+1}/{total}] 检测中...")
        
        # 加载数据
        try:
            df = pd.read_csv(csv_file)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
        except Exception as e:
            continue
        
        if len(df) < 60:
            continue
        
        # 检测所有形态（使用 2019-2025 年数据计算，但只统计 2020-2025 年买入信号）
        try:
            all_patterns = backtester.check_all_patterns(df, code, '2019-01-01', '2025-12-31')
            
            if all_patterns and len(all_patterns) > 0:
                print(f"[{idx+1}/{total}] {code} - ✅ 发现 {len(all_patterns)} 个形态")
                
                # 对每个形态模拟交易
                for pattern_idx, pattern_info in enumerate(all_patterns):
                    trade_result = backtester.simulate_trade(df, pattern_info)
                    
                    if trade_result:
                        buy_date = pd.to_datetime(trade_result.get('buy_date', ''))
                        
                        # V2.2: 只统计 2020 年及以后的信号
                        if buy_date.year < 2020:
                            continue
                        
                        signal = {
                            'stock_code': code,
                            'stock_name': name,
                            'pattern_index': pattern_idx + 1,
                            'buy_date': str(trade_result.get('buy_date', '')),
                            'sell_date': str(trade_result.get('sell_date', '')),
                            'buy_price': trade_result.get('buy_price', 0),
                            'sell_price': trade_result.get('sell_price', 0),
                            'profit_pct': trade_result.get('net_return', 0) * 100,
                            'hold_days': trade_result.get('holding_days', 0),
                            'exit_reason': trade_result.get('sell_reason', ''),
                            'retrace_date': str(pattern_info.get('retrace_date', '')),
                            'surge_date': str(pattern_info.get('surge_date', '')),
                            'support_level': pattern_info.get('support_level', 0),
                        }
                        all_signals.append(signal)
        except Exception as e:
            continue
    
    print()
    print("=" * 80)
    print(f"回测完成：共检测 {total} 只股票，发现 {len(all_signals)} 个形态信号")
    print("=" * 80)
    
    # 统计分析
    if all_signals:
        profits = [s['profit_pct'] for s in all_signals]
        avg_profit = sum(profits) / len(profits)
        max_profit = max(profits)
        min_profit = min(profits)
        profitable = len([p for p in profits if p > 0])
        win_rate = profitable / len(all_signals) * 100
        
        # 添加年份字段
        for signal in all_signals:
            if signal['buy_date']:
                try:
                    buy_year = pd.to_datetime(signal['buy_date']).year
                    signal['buy_year'] = buy_year
                except:
                    signal['buy_year'] = None
        
        year_counts = Counter([s['buy_year'] for s in all_signals if s['buy_year']])
        years = sorted(year_counts.keys())
        
        # 计算年均信号
        avg_signals_per_year = len(all_signals) / len(years) if years else 0
        
        print()
        print("📊 V2.2 放宽参数版回测结果（2020-2025 年）:")
        print(f"  总信号数：{len(all_signals)} 个")
        print(f"  年均信号：{avg_signals_per_year:.1f} 个/年")
        print(f"  平均收益：{avg_profit:.2f}%")
        print(f"  最高收益：{max_profit:.2f}%")
        print(f"  最低收益：{min_profit:.2f}%")
        print(f"  胜率：{win_rate:.1f}%")
        print()
        
        # 按年份详细统计
        if years:
            print("  按年份统计:")
            for year in years:
                year_signals = [s for s in all_signals if s['buy_year'] == year]
                year_profits = [s['profit_pct'] for s in year_signals]
                avg_year_profit = sum(year_profits) / len(year_profits) if year_profits else 0
                year_profitable = len([p for p in year_profits if p > 0])
                year_win_rate = year_profitable / len(year_signals) * 100 if year_signals else 0
                print(f"    {year}年：{len(year_signals)} 个信号，平均收益 {avg_year_profit:.2f}%, 胜率 {year_win_rate:.1f}%")
        
        # 保存结果
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = Path(f'backtest_results/backtest_v22_relaxed_{timestamp}.json')
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(all_signals, f, ensure_ascii=False, indent=2, default=str)
        
        print(f"\n✅ 结果已保存：{output_file}")
        
        # 保存 CSV
        csv_file = Path(f'backtest_results/backtest_v22_relaxed_{timestamp}.csv')
        df_signals = pd.DataFrame(all_signals)
        df_signals.to_csv(csv_file, index=False, encoding='utf-8-sig')
        print(f"✅ CSV 已保存：{csv_file}")
        
        # 判断是否需要进一步放宽参数
        print()
        print("=" * 80)
        if avg_signals_per_year < 10:
            print("⚠️  警告：年均信号仍然过少（<10 个/年）")
            print("建议：进一步放宽参数或调整策略")
        elif avg_signals_per_year < 20:
            print("✅ 信号数量适中（10-20 个/年）")
            print("参数设置合理")
        else:
            print("✅ 信号数量充足（≥20 个/年）")
            print("参数设置合理")
        print("=" * 80)
        
    else:
        print("\n❌ 没有发现任何形态信号")
    
    return all_signals


if __name__ == '__main__':
    run_v22_relaxed_backtest()
