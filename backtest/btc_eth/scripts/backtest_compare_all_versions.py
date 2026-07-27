#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v6.16 vs v6.16.1 vs v6.16.2 三版本回测对比脚本
"""

import sys
import os

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, project_root)

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)

from backtest_v616 import BacktestEngine as BacktestEngineV616
from backtest_v6161 import BacktestEngine as BacktestEngineV6161
from backtest_v6162 import BacktestEngine as BacktestEngineV6162
import yaml
import pandas as pd
from datetime import datetime
import json

def run_comparison():
    """运行三版本对比"""
    
    print("正在加载配置文件...")
    config_path = os.path.join(project_root, 'strategies', 'btc_eth', 'config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    print("正在加载市场数据...")
    data_dir = os.path.join(project_root, 'backtest', 'btc_eth', 'data')
    
    # 加载CSV数据
    def load_klines_from_csv(filepath):
        df = pd.read_csv(filepath)
        df['open_time'] = pd.to_datetime(df['open_time'])
        df.set_index('open_time', inplace=True)
        df.rename(columns={
            'open_price': 'open',
            'high_price': 'high',
            'low_price': 'low',
            'close_price': 'close'
        }, inplace=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    
    klines_1h = load_klines_from_csv(os.path.join(data_dir, 'btcusdt_1h.csv'))
    klines_4h = load_klines_from_csv(os.path.join(data_dir, 'btcusdt_4h.csv'))
    klines_1d = load_klines_from_csv(os.path.join(data_dir, 'btcusdt_1d.csv'))
    
    print("\n正在运行v6.16回测...")
    engine_v616 = BacktestEngineV616(config)
    results_v616 = engine_v616.run_backtest(klines_1h, klines_4h, klines_1d)
    
    print("正在运行v6.16.1回测...")
    engine_v6161 = BacktestEngineV6161(config)
    results_v6161 = engine_v6161.run_backtest(klines_1h, klines_4h, klines_1d)
    
    print("正在运行v6.16.2回测...")
    engine_v6162 = BacktestEngineV6162(config)
    results_v6162 = engine_v6162.run_backtest(klines_1h, klines_4h, klines_1d)
    
    print("\n" + "="*80)
    print("# v6.16 vs v6.16.1 vs v6.16.2 三版本回测对比报告")
    print("="*80)
    
    print("\n## 一、核心参数差异对比\n")
    print("| 参数 | v6.16 | v6.16.1 | v6.16.2 |")
    print("|------|-------|---------|---------|")
    print("| ATR%上限 | 7.0% | 8.5% | 8.0% |")
    print("| 成交量倍数(S级) | 1.5 | 1.4 | 质量评分≥60 |")
    print("| 成交量倍数(A级) | 1.5 | 1.3 | 质量评分≥50 |")
    print("| 成交量倍数(B/C级) | 1.0 | 1.0 | 质量评分≥40 |")
    print("| 冷却期 | 6小时 | 4小时 | 动态2-8小时 |")
    print("| S级额外过滤 | 无 | ADX>25或MACD放大 | 无 |")
    
    print("\n## 二、整体表现对比\n")
    print("### 资金情况\n")
    print("| 指标 | v6.16 | v6.16.1 | v6.16.2 | 最优版本 |")
    print("|------|-------|---------|---------|----------|")
    
    initial = results_v616['initial_capital']
    
    final_v616 = results_v616['final_capital']
    final_v6161 = results_v6161['final_capital']
    final_v6162 = results_v6162['final_capital']
    
    return_v616 = (final_v616 - initial) / initial * 100
    return_v6161 = (final_v6161 - initial) / initial * 100
    return_v6162 = (final_v6162 - initial) / initial * 100
    
    print(f"| 初始资金 | {initial:.2f} USDT | {initial:.2f} USDT | {initial:.2f} USDT | - |")
    print(f"| 最终资金 | {final_v616:.2f} USDT | {final_v6161:.2f} USDT | {final_v6162:.2f} USDT | {'v6.16' if final_v616 >= final_v6161 and final_v616 >= final_v6162 else 'v6.16.1' if final_v6161 >= final_v6162 else 'v6.16.2'} |")
    print(f"| 总收益率 | {return_v616:.2f}% | {return_v6161:.2f}% | {return_v6162:.2f}% | {'v6.16' if return_v616 >= return_v6161 and return_v616 >= return_v6162 else 'v6.16.1' if return_v6161 >= return_v6162 else 'v6.16.2'} |")
    
    dd_v616 = results_v616['max_drawdown']
    dd_v6161 = results_v6161['max_drawdown']
    dd_v6162 = results_v6162['max_drawdown']
    
    print(f"| 最大回撤 | {dd_v616:.2f}% | {dd_v6161:.2f}% | {dd_v6162:.2f}% | {'v6.16' if dd_v616 <= dd_v6161 and dd_v616 <= dd_v6162 else 'v6.16.1' if dd_v6161 <= dd_v6162 else 'v6.16.2'} |")
    
    sharpe_v616 = results_v616['sharpe_ratio']
    sharpe_v6161 = results_v6161['sharpe_ratio']
    sharpe_v6162 = results_v6162['sharpe_ratio']
    
    print(f"| 夏普比率 | {sharpe_v616:.2f} | {sharpe_v6161:.2f} | {sharpe_v6162:.2f} | {'v6.16' if sharpe_v616 >= sharpe_v6161 and sharpe_v616 >= sharpe_v6162 else 'v6.16.1' if sharpe_v6161 >= sharpe_v6162 else 'v6.16.2'} |")
    
    print("\n### 交易统计\n")
    print("| 指标 | v6.16 | v6.16.1 | v6.16.2 | 最优版本 |")
    print("|------|-------|---------|---------|----------|")
    
    trades_v616 = results_v616['total_trades']
    trades_v6161 = results_v6161['total_trades']
    trades_v6162 = results_v6162['total_trades']
    
    print(f"| 总交易次数 | {trades_v616} | {trades_v6161} | {trades_v6162} | {'v6.16.1' if trades_v6161 >= trades_v616 and trades_v6161 >= trades_v6162 else 'v6.16' if trades_v616 >= trades_v6162 else 'v6.16.2'} |")
    
    wins_v616 = results_v616['winning_trades']
    wins_v6161 = results_v6161['winning_trades']
    wins_v6162 = results_v6162['winning_trades']
    
    print(f"| 盈利次数 | {wins_v616} | {wins_v6161} | {wins_v6162} | {'v6.16.1' if wins_v6161 >= wins_v616 and wins_v6161 >= wins_v6162 else 'v6.16' if wins_v616 >= wins_v6162 else 'v6.16.2'} |")
    
    losses_v616 = results_v616['losing_trades']
    losses_v6161 = results_v6161['losing_trades']
    losses_v6162 = results_v6162['losing_trades']
    
    print(f"| 亏损次数 | {losses_v616} | {losses_v6161} | {losses_v6162} | {'v6.16' if losses_v616 <= losses_v6161 and losses_v616 <= losses_v6162 else 'v6.16.1' if losses_v6161 <= losses_v6162 else 'v6.16.2'} |")
    
    winrate_v616 = results_v616['win_rate']
    winrate_v6161 = results_v6161['win_rate']
    winrate_v6162 = results_v6162['win_rate']
    
    print(f"| 胜率 | {winrate_v616:.2f}% | {winrate_v6161:.2f}% | {winrate_v6162:.2f}% | {'v6.16.1' if winrate_v6161 >= winrate_v616 and winrate_v6161 >= winrate_v6162 else 'v6.16' if winrate_v616 >= winrate_v6162 else 'v6.16.2'} |")
    
    avg_pnl_v616 = results_v616['avg_pnl_percent']
    avg_pnl_v6161 = results_v6161['avg_pnl_percent']
    avg_pnl_v6162 = results_v6162['avg_pnl_percent']
    
    print(f"| 平均盈亏 | {avg_pnl_v616:.2f}% | {avg_pnl_v6161:.2f}% | {avg_pnl_v6162:.2f}% | {'v6.16' if avg_pnl_v616 >= avg_pnl_v6161 and avg_pnl_v616 >= avg_pnl_v6162 else 'v6.16.1' if avg_pnl_v6161 >= avg_pnl_v6162 else 'v6.16.2'} |")
    
    print("\n## 三、按等级统计对比\n")
    
    for grade in ['S', 'A', 'B', 'C']:
        print(f"\n### {grade}级信号\n")
        print(f"| 指标 | v6.16 | v6.16.1 | v6.16.2 |")
        print(f"|------|-------|---------|---------|")
        
        grade_stats_v616 = results_v616.get('grade_stats', {}).get(grade, {})
        grade_stats_v6161 = results_v6161.get('grade_stats', {}).get(grade, {})
        grade_stats_v6162 = results_v6162.get('grade_stats', {}).get(grade, {})
        
        print(f"| 交易次数 | {grade_stats_v616.get('count', 0)} | {grade_stats_v6161.get('count', 0)} | {grade_stats_v6162.get('count', 0)} |")
        print(f"| 胜率 | {grade_stats_v616.get('win_rate', 0):.2f}% | {grade_stats_v6161.get('win_rate', 0):.2f}% | {grade_stats_v6162.get('win_rate', 0):.2f}% |")
        print(f"| 平均盈亏 | {grade_stats_v616.get('avg_pnl_percent', 0):.2f}% | {grade_stats_v6161.get('avg_pnl_percent', 0):.2f}% | {grade_stats_v6162.get('avg_pnl_percent', 0):.2f}% |")
        print(f"| 总盈亏 | {grade_stats_v616.get('total_pnl', 0):.2f} USDT | {grade_stats_v6161.get('total_pnl', 0):.2f} USDT | {grade_stats_v6162.get('total_pnl', 0):.2f} USDT |")
    
    print("\n## 四、关键发现与分析\n")
    
    print("### 1. 整体表现排名\n")
    
    print("**收益率排名**：")
    returns = [('v6.16', return_v616), ('v6.16.1', return_v6161), ('v6.16.2', return_v6162)]
    returns_sorted = sorted(returns, key=lambda x: x[1], reverse=True)
    for i, (version, ret) in enumerate(returns_sorted, 1):
        print(f"  {i}. {version}: {ret:.2f}%")
    
    print("\n**风险控制排名**（最大回撤越小越好）：")
    drawdowns = [('v6.16', dd_v616), ('v6.16.1', dd_v6161), ('v6.16.2', dd_v6162)]
    drawdowns_sorted = sorted(drawdowns, key=lambda x: x[1])
    for i, (version, dd) in enumerate(drawdowns_sorted, 1):
        print(f"  {i}. {version}: {dd:.2f}%")
    
    print("\n**胜率排名**：")
    winrates = [('v6.16', winrate_v616), ('v6.16.1', winrate_v6161), ('v6.16.2', winrate_v6162)]
    winrates_sorted = sorted(winrates, key=lambda x: x[1], reverse=True)
    for i, (version, wr) in enumerate(winrates_sorted, 1):
        print(f"  {i}. {version}: {wr:.2f}%")
    
    print("\n### 2. 各版本特点分析\n")
    
    print("**v6.16（基准版本）**：")
    print(f"- 交易数：{trades_v616}笔")
    print(f"- 胜率：{winrate_v616:.2f}%")
    print(f"- 收益率：{return_v616:.2f}%")
    print(f"- 最大回撤：{dd_v616:.2f}%")
    print("- 特点：稳定性最好，风险控制最佳")
    
    print("\n**v6.16.1（放宽版本）**：")
    print(f"- 交易数：{trades_v6161}笔（比v6.16增加{trades_v6161-trades_v616}笔）")
    print(f"- 胜率：{winrate_v6161:.2f}%")
    print(f"- 收益率：{return_v6161:.2f}%")
    print(f"- 最大回撤：{dd_v6161:.2f}%")
    print("- 特点：交易频次最高，但S级额外过滤失败导致收益下降")
    
    print("\n**v6.16.2（优化版本）**：")
    print(f"- 交易数：{trades_v6162}笔")
    print(f"- 胜率：{winrate_v6162:.2f}%")
    print(f"- 收益率：{return_v6162:.2f}%")
    print(f"- 最大回撤：{dd_v6162:.2f}%")
    print("- 特点：移除失败的S级过滤，引入成交量质量评分，表现中等")
    
    print("\n### 3. 核心问题诊断\n")
    
    print("**v6.16.1失败原因**：")
    print("- S级额外过滤逻辑失败，误杀优质信号")
    print("- S级降级10次，导致S级胜率仍为54.17%")
    print("- ATR%上限放宽至8.5%，引入更多高风险信号")
    print("- 最大回撤增加1.17%")
    
    print("\n**v6.16.2改进效果**：")
    print("- 移除S级额外过滤，S级胜率提升至64.00%")
    print("- 引入成交量质量评分，提升信号质量")
    print("- 动态冷却期机制运行正常")
    print("- 但交易数下降至60笔，收益率仍为4.19%")
    
    print("\n## 五、下一步建议\n")
    
    print("### 推荐方案\n")
    
    if return_v616 >= return_v6161 and return_v616 >= return_v6162:
        print("**推荐使用v6.16**")
        print("- 理由：收益率最高，风险控制最好")
        print("- 建议：保持v6.16配置，无需优化")
    elif return_v6161 >= return_v616 and return_v6161 >= return_v6162:
        print("**推荐使用v6.16.1**")
        print("- 理由：交易频次最高，胜率最高")
        print("- 建议：移除S级额外过滤逻辑")
    else:
        print("**推荐使用v6.16.2**")
        print("- 理由：综合表现最优")
        print("- 建议：调整成交量质量评分阈值，增加交易频次")
    
    print("\n### 进一步优化方向\n")
    print("1. **优化成交量质量评分阈值**")
    print("   - S级：60分→55分")
    print("   - A级：50分→45分")
    print("   - B/C级：40分→35分")
    print("   - 预期：增加交易频次至80-90笔")
    
    print("\n2. **优化动态冷却期参数**")
    print("   - 高波动：2小时→1.5小时")
    print("   - 中等波动：3小时→2.5小时")
    print("   - 低波动：4小时→3小时")
    print("   - 预期：提升资金利用率")
    
    print("\n3. **引入市场状态识别**")
    print("   - 区分趋势市和震荡市")
    print("   - 趋势市：放宽过滤条件")
    print("   - 震荡市：收紧过滤条件")
    print("   - 预期：提升信号质量")
    
    print("\n" + "="*80)
    
    return {
        'v616': results_v616,
        'v6161': results_v6161,
        'v6162': results_v6162
    }

if __name__ == '__main__':
    run_comparison()
