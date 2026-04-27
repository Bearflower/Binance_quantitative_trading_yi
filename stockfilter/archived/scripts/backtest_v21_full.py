#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.1 完整回测（检测所有形态，不只是第一次）
返回 2019-2026 年所有符合条件的形态
"""

import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict

from backtester_scheme_ab import BacktesterWithRules_AB
from utils.logger import get_logger

logger = get_logger()


def run_full_backtest():
    """运行完整回测（检测所有形态）"""
    
    print("=" * 80)
    print("股票形态策略 V2.1 完整回测（检测所有形态）")
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
                
                # 对每个形态模拟交易
                for pattern_idx, pattern_info in enumerate(all_patterns):
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
            else:
                print("❌ 不满足")
        except Exception as e:
            print(f"❌ 异常：{e}")
    
    print()
    print("=" * 80)
    print(f"回测完成：共检测 {total} 只股票，发现 {len(all_signals)} 个形态信号")
    print("=" * 80)
    
    # 统计
    if all_signals:
        profits = [s['profit_pct'] for s in all_signals]
        avg_profit = sum(profits) / len(profits)
        max_profit = max(profits)
        min_profit = min(profits)
        profitable = len([p for p in profits if p > 0])
        win_rate = profitable / len(all_signals) * 100
        
        print(f"\n统计结果:")
        print(f"  总信号数：{len(all_signals)} 个")
        print(f"  平均收益：{avg_profit:.2f}%")
        print(f"  最高收益：{max_profit:.2f}%")
        print(f"  最低收益：{min_profit:.2f}%")
        print(f"  胜率：{win_rate:.1f}%")
        
        # 按年份统计
        print(f"\n按买入年份统计:")
        for signal in all_signals:
            if signal['buy_date']:
                try:
                    buy_year = pd.to_datetime(signal['buy_date']).year
                    signal['buy_year'] = buy_year
                except:
                    signal['buy_year'] = None
        
        from collections import Counter
        year_counts = Counter([s['buy_year'] for s in all_signals if s['buy_year']])
        for year in sorted(year_counts.keys()):
            year_signals = [s for s in all_signals if s['buy_year'] == year]
            year_profits = [s['profit_pct'] for s in year_signals]
            print(f"  {year}年：{len(year_signals)} 个信号，平均收益 {sum(year_profits)/len(year_profits):.2f}%")
        
        # 保存结果
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = Path(f'backtest_results/backtest_v21_full_{timestamp}.json')
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(all_signals, f, ensure_ascii=False, indent=2, default=str)
        
        print(f"\n✅ 结果已保存：{output_file}")
        
        # 保存 CSV
        csv_file = Path(f'backtest_results/backtest_v21_full_{timestamp}.csv')
        df_signals = pd.DataFrame(all_signals)
        df_signals.to_csv(csv_file, index=False, encoding='utf-8-sig')
        print(f"✅ CSV 已保存：{csv_file}")
    else:
        print("\n❌ 没有发现任何形态信号")
    
    return all_signals


if __name__ == '__main__':
    run_full_backtest()
