#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.3 回测器执行脚本
- 进一步放宽条件增加信号数量
- 目标：每月 2-3 个信号（每年 24-36 个）
- 特别关注排除 2019 年后的信号数量
"""

import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict
import os

from backtester_v23 import BacktesterV23
from utils.logger import get_logger

logger = get_logger()


def load_all_stock_data(data_dir: str = 'data/backtest/baostocks_full') -> List[Dict]:
    """加载所有本地股票数据"""
    stock_data = []
    
    if not os.path.exists(data_dir):
        logger.error(f"数据目录不存在：{data_dir}")
        return stock_data
    
    for file in os.listdir(data_dir):
        if file.endswith('_data.csv'):
            code = file.replace('_data.csv', '')
            file_path = os.path.join(data_dir, file)
            
            try:
                df = pd.read_csv(file_path)
                df['date'] = pd.to_datetime(df['date'])
                df = df.sort_values('date').reset_index(drop=True)
                
                stock_data.append({
                    'code': code,
                    'data': df
                })
                logger.info(f"加载 {code} 数据，共 {len(df)} 条")
            except Exception as e:
                logger.error(f"加载 {code} 数据失败：{e}")
    
    return stock_data


def run_backtest_v23(
    period_start: str = '2019-01-01',
    period_end: str = '2026-04-07',
    data_dir: str = 'data/backtest/baostocks_full'
):
    """运行 V2.3 回测"""
    logger.info("="*80)
    logger.info("开始 V2.3 回测（进一步放宽条件增加信号）")
    logger.info("="*80)
    logger.info("V2.3 参数设置：")
    logger.info("  - 缩量到放量时间窗口：40 天（从 25 天放宽）")
    logger.info("  - 跌幅阈值：10%（从 12% 放宽）")
    logger.info("  - 放量涨幅：4%（从 5% 放宽）")
    logger.info("  - 量比最小值：1.3（从 1.5 放宽）")
    logger.info("  - 缩量比率：70%（从 60% 放宽）")
    logger.info("目标：每月 2-3 个信号（每年 24-36 个）")
    logger.info("="*80)
    
    # 加载数据
    logger.info(f"加载本地数据：{data_dir}")
    stock_data = load_all_stock_data(data_dir)
    logger.info(f"共加载 {len(stock_data)} 只股票数据")
    
    if not stock_data:
        logger.error("没有加载到任何股票数据")
        return
    
    # 创建回测器
    backtester = BacktesterV23('config_v21_final.yaml')
    
    # 回测结果
    all_patterns = []
    all_trades = []
    
    # 遍历所有股票
    for stock in stock_data:
        code = stock['code']
        df = stock['data']
        
        # 检测所有形态
        patterns = backtester.check_all_patterns(df, code, period_start, period_end)
        
        if patterns:
            logger.info(f"{code}: 检测到 {len(patterns)} 个形态")
            all_patterns.extend(patterns)
            
            # 对每个形态模拟交易
            for pattern in patterns:
                trade = backtester.simulate_trade(df, pattern)
                if trade:
                    all_trades.append(trade)
    
    # 统计结果
    logger.info("="*80)
    logger.info("V2.3 回测结果统计")
    logger.info("="*80)
    
    total_stocks = len(stock_data)
    matched_stocks = len(set(p['code'] for p in all_patterns))
    total_patterns = len(all_patterns)
    total_trades = len(all_trades)
    
    logger.info(f"检测股票数：{total_stocks}")
    logger.info(f"满足形态股票数：{matched_stocks}")
    logger.info(f"总形态数：{total_patterns}")
    logger.info(f"总交易数：{total_trades}")
    
    if all_trades:
        # 计算收益统计
        net_returns = [t['net_return'] for t in all_trades]
        avg_return = sum(net_returns) / len(net_returns) * 100
        win_count = sum(1 for r in net_returns if r > 0)
        win_rate = win_count / len(net_returns) * 100
        max_return = max(net_returns) * 100
        min_return = min(net_returns) * 100
        
        logger.info(f"平均收益：{avg_return:.2f}%")
        logger.info(f"胜率：{win_rate:.2f}%")
        logger.info(f"最高收益：{max_return:.2f}%")
        logger.info(f"最低收益：{min_return:.2f}%")
    
    # 按年份统计
    logger.info("\n" + "="*80)
    logger.info("按年份统计（全部数据）")
    logger.info("="*80)
    
    yearly_stats = {}
    for pattern in all_patterns:
        year = pattern['retrace_date'][:4]
        yearly_stats[year] = yearly_stats.get(year, 0) + 1
    
    for year in sorted(yearly_stats.keys()):
        logger.info(f"{year}年：{yearly_stats[year]} 个形态")
    
    # 排除 2019 年后的统计
    logger.info("\n" + "="*80)
    logger.info("排除 2019 年后的统计（2020-2026）")
    logger.info("="*80)
    
    patterns_exclude_2019 = [p for p in all_patterns if p['retrace_date'][:4] != '2019']
    trades_exclude_2019 = [t for t in all_trades if t['buy_date'][:4] != '2019']
    
    logger.info(f"排除 2019 年后形态数：{len(patterns_exclude_2019)}")
    logger.info(f"排除 2019 年后交易数：{len(trades_exclude_2019)}")
    
    # 按年份统计（排除 2019）
    yearly_exclude_2019 = {}
    for pattern in patterns_exclude_2019:
        year = pattern['retrace_date'][:4]
        yearly_exclude_2019[year] = yearly_exclude_2019.get(year, 0) + 1
    
    logger.info("\n年份分布（排除 2019）：")
    for year in sorted(yearly_exclude_2019.keys()):
        logger.info(f"{year}年：{yearly_exclude_2019[year]} 个形态")
    
    # 计算每月平均信号数
    if patterns_exclude_2019:
        # 确定数据时间范围
        dates = [p['retrace_date'] for p in patterns_exclude_2019]
        min_date = min(dates)
        max_date = max(dates)
        
        # 计算月份数
        min_dt = pd.to_datetime(min_date)
        max_dt = pd.to_datetime(max_date)
        months_diff = (max_dt.year - min_dt.year) * 12 + (max_dt.month - min_dt.month) + 1
        
        avg_per_month = len(patterns_exclude_2019) / months_diff
        logger.info(f"\n数据时间范围：{min_date} 到 {max_date}")
        logger.info(f"月份数：{months_diff} 个月")
        logger.info(f"平均每月信号数：{avg_per_month:.2f} 个/月")
        logger.info(f"平均每年信号数：{avg_per_month * 12:.2f} 个/年")
        
        # 检查是否达到目标（每月 2-3 个）
        if avg_per_month >= 2:
            logger.info(f"✅ 达到目标：每月 {avg_per_month:.2f} 个信号（目标：2-3 个）")
        else:
            logger.info(f"⚠️  未达到目标：每月 {avg_per_month:.2f} 个信号（目标：2-3 个）")
    
    if trades_exclude_2019:
        # 计算收益统计（排除 2019）
        net_returns_excl = [t['net_return'] for t in trades_exclude_2019]
        avg_return_excl = sum(net_returns_excl) / len(net_returns_excl) * 100
        win_count_excl = sum(1 for r in net_returns_excl if r > 0)
        win_rate_excl = win_count_excl / len(net_returns_excl) * 100
        
        logger.info(f"\n排除 2019 年后的收益统计：")
        logger.info(f"平均收益：{avg_return_excl:.2f}%")
        logger.info(f"胜率：{win_rate_excl:.2f}%")
    
    # 保存结果
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = Path('backtest_results')
    output_dir.mkdir(exist_ok=True)
    
    # 保存 JSON
    json_file = output_dir / f'backtest_v23_{timestamp}.json'
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump({
            'patterns': all_patterns,
            'trades': all_trades,
            'summary': {
                'total_stocks': total_stocks,
                'matched_stocks': matched_stocks,
                'total_patterns': total_patterns,
                'total_trades': total_trades,
                'avg_return': avg_return if all_trades else 0,
                'win_rate': win_rate if all_trades else 0,
                'max_return': max_return if all_trades else 0,
                'min_return': min_return if all_trades else 0,
                'patterns_exclude_2019': len(patterns_exclude_2019),
                'trades_exclude_2019': len(trades_exclude_2019),
                'avg_monthly_signals': avg_per_month if patterns_exclude_2019 else 0
            },
            'yearly_stats': yearly_stats,
            'yearly_exclude_2019': yearly_exclude_2019
        }, f, ensure_ascii=False, default=str)
    logger.info(f"结果保存到：{json_file}")
    
    # 保存 CSV
    if all_trades:
        csv_file = output_dir / f'backtest_v23_{timestamp}.csv'
        trades_df = pd.DataFrame(all_trades)
        trades_df.to_csv(csv_file, index=False, encoding='utf-8-sig')
        logger.info(f"交易记录保存到：{csv_file}")
    
    # 显示部分形态示例
    if all_patterns:
        logger.info("\n" + "="*80)
        logger.info("形态示例（前 10 个）：")
        logger.info("="*80)
        for i, pattern in enumerate(all_patterns[:10], 1):
            logger.info(f"\n形态 {i}:")
            logger.info(f"  代码：{pattern['code']}")
            logger.info(f"  下跌：{pattern['drop_start_date']} 到 {pattern['drop_end_date']} ({pattern['drop_change']*100:.2f}%)")
            logger.info(f"  缩量：{pattern['shrink_date']}")
            logger.info(f"  放量：{pattern['surge_date']} ({pattern['surge_close']:.2f})")
            logger.info(f"  回踩：{pattern['retrace_date']} ({pattern['retrace_close']:.2f})")
            logger.info(f"  方案：{pattern['scheme']}")
            logger.info(f"  参数：跌幅{pattern['drop_threshold']*100}%, 涨幅{pattern['surge_price_ratio']*100}%, 量比{pattern['min_volume_ratio']}")


if __name__ == '__main__':
    # 运行回测（使用 baostocks_full 完整数据）
    run_backtest_v23(
        period_start='2019-01-01',
        period_end='2026-04-07',
        data_dir='data/backtest/baostocks_full'
    )
