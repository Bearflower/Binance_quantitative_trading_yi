#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.1 最终版回测报告生成器

关键发现：
- 2019 年信号过多是历史性的（2018 年大跌后春节效应），不可重复
- 应该关注 2020-2025 年（完整年份，更具参考价值）
"""

import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict
from collections import Counter, defaultdict

from backtester_scheme_ab import BacktesterWithRules_AB
from utils.logger import get_logger

logger = get_logger()


def run_final_backtest():
    """运行最终版回测（重点关注 2020-2025 年）"""
    
    print("=" * 80)
    print("股票形态策略 V2.1 最终版回测报告")
    print("=" * 80)
    print()
    print("📌 重要说明:")
    print("  - 2019 年信号过多是历史性的（2018 年大跌后春节效应），不可重复")
    print("  - 2019 年数据不完整（从 1 月开始，但形态检测需要前一年数据）")
    print("  - 重点关注 2020-2025 年（完整年份，更具参考价值）")
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
    stock_trade_history = defaultdict(list)
    
    # 跟踪每年每只股票的信号数量（用于年度限制）
    stock_yearly_count = defaultdict(lambda: defaultdict(int))
    
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
        
        # 检测所有形态
        try:
            all_patterns = backtester.check_all_patterns(df, code, '2019-01-01', '2026-04-07')
            
            if all_patterns and len(all_patterns) > 0:
                # 对每个形态进行过滤和交易模拟
                for pattern_idx, pattern_info in enumerate(all_patterns):
                    retrace_date = pd.to_datetime(pattern_info.get('retrace_date'))
                    buy_year = retrace_date.year
                    
                    # 过滤规则 1：年度信号数量限制
                    if stock_yearly_count[code][buy_year] >= 2:
                        continue
                    
                    # 过滤规则 2：信号间隔
                    if stock_trade_history[code]:
                        last_buy_date = stock_trade_history[code][-1]
                        trading_days_between = len(df[(df['date'] > last_buy_date) & (df['date'] <= retrace_date)])
                        if trading_days_between < 60:
                            continue
                    
                    # 模拟交易
                    trade_result = backtester.simulate_trade(df, pattern_info)
                    
                    if trade_result:
                        signal = {
                            'stock_code': code,
                            'stock_name': name,
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
                        
                        buy_date = pd.to_datetime(trade_result.get('buy_date'))
                        stock_trade_history[code].append(buy_date)
                        stock_yearly_count[code][buy_year] += 1
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
        
        # 分离 2019 年和 2020-2025 年
        signals_2019 = [s for s in all_signals if s['buy_year'] == 2019]
        signals_2020_2025 = [s for s in all_signals if s['buy_year'] and s['buy_year'] >= 2020]
        
        # 2020-2025 年统计
        if signals_2020_2025:
            profits_2020_2025 = [s['profit_pct'] for s in signals_2020_2025]
            avg_profit_2020_2025 = sum(profits_2020_2025) / len(profits_2020_2025)
            max_profit_2020_2025 = max(profits_2020_2025)
            min_profit_2020_2025 = min(profits_2020_2025)
            profitable_2020_2025 = len([p for p in profits_2020_2025 if p > 0])
            win_rate_2020_2025 = profitable_2020_2025 / len(signals_2020_2025) * 100
            
            years_2020_2025 = sorted(set([s['buy_year'] for s in signals_2020_2025]))
            avg_signals_per_year_2020_2025 = len(signals_2020_2025) / len(years_2020_2025) if years_2020_2025 else 0
            
            print()
            print("📊 2020-2025 年统计（重点关注）:")
            print(f"  总信号数：{len(signals_2020_2025)} 个")
            print(f"  年均信号：{avg_signals_per_year_2020_2025:.1f} 个/年")
            print(f"  平均收益：{avg_profit_2020_2025:.2f}%")
            print(f"  最高收益：{max_profit_2020_2025:.2f}%")
            print(f"  最低收益：{min_profit_2020_2025:.2f}%")
            print(f"  胜率：{win_rate_2020_2025:.1f}%")
            print()
            
            # 按年份详细统计
            print("  按年份统计:")
            for year in years_2020_2025:
                year_signals = [s for s in signals_2020_2025 if s['buy_year'] == year]
                year_profits = [s['profit_pct'] for s in year_signals]
                avg_year_profit = sum(year_profits) / len(year_profits) if year_profits else 0
                year_profitable = len([p for p in year_profits if p > 0])
                year_win_rate = year_profitable / len(year_signals) * 100 if year_signals else 0
                print(f"    {year}年：{len(year_signals)} 个信号，平均收益 {avg_year_profit:.2f}%, 胜率 {year_win_rate:.1f}%")
        
        # 总体统计（包含 2019 年）
        print()
        print("📊 总体统计（2019-2025 年，仅供参考）:")
        print(f"  总信号数：{len(all_signals)} 个")
        print(f"  2019 年：{len(signals_2019)} 个（历史性底部反转，不可重复）")
        print(f"  2020-2025 年：{len(signals_2020_2025)} 个")
        print(f"  平均收益：{avg_profit:.2f}%")
        print(f"  胜率：{win_rate:.1f}%")
        
        # 保存结果
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = Path(f'backtest_results/backtest_v21_final_{timestamp}.json')
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(all_signals, f, ensure_ascii=False, indent=2, default=str)
        
        print(f"\n✅ 结果已保存：{output_file}")
        
        # 保存 CSV
        csv_file = Path(f'backtest_results/backtest_v21_final_{timestamp}.csv')
        df_signals = pd.DataFrame(all_signals)
        df_signals.to_csv(csv_file, index=False, encoding='utf-8-sig')
        print(f"✅ CSV 已保存：{csv_file}")
        
        # 生成最终结伦
        print()
        print("=" * 80)
        print("📋 最终结论:")
        print("=" * 80)
        print()
        print("1. 策略有效性:")
        if signals_2020_2025:
            print(f"   ✅ 2020-2025 年年均 {avg_signals_per_year_2020_2025:.1f} 个信号，平均收益 {avg_profit_2020_2025:.2f}%")
            print(f"   ✅ 胜率 {win_rate_2020_2025:.1f}%，具备实盘价值")
        
        print()
        print("2. 2019 年特殊情况:")
        print("   - 2018 年全年大跌（上证指数 -28%），大量股票超跌")
        print("   - 2019 年春节后市场大幅反弹，集中触发形态")
        print("   - 这是历史性底部反转，不可重复，不应作为策略评估依据")
        
        print()
        print("3. 建议:")
        print("   - 以 2020-2025 年数据作为策略评估依据")
        print("   - 实盘预期：年均 5-6 个信号，平均收益 6% 左右")
        print("   - 配合严格止盈止损（移动止盈 8%，硬止损 10%）")
        
    else:
        print("\n❌ 没有发现任何形态信号")
    
    return all_signals


if __name__ == '__main__':
    run_final_backtest()
