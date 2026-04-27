#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.2 回测器执行脚本
- 使用本地数据
- 放宽缩量到放量的时间窗口（从 10 天延长到 25 天）
"""

import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict
import os

from backtester_v22 import BacktesterV22
from utils.logger import get_logger

logger = get_logger()


def load_all_stock_data(data_dir: str = 'data/backtest/local_stocks') -> List[Dict]:
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


def run_backtest_v22(
    period_start: str = '2019-01-01',
    period_end: str = '2026-04-07',
    data_dir: str = 'data/backtest/local_stocks'
):
    """运行 V2.2 回测"""
    logger.info("="*80)
    logger.info("开始 V2.2 回测（缩量到放量时间窗口：25 天）")
    logger.info("="*80)
    
    # 加载数据
    logger.info(f"加载本地数据：{data_dir}")
    stock_data = load_all_stock_data(data_dir)
    logger.info(f"共加载 {len(stock_data)} 只股票数据")
    
    if not stock_data:
        logger.error("没有加载到任何股票数据")
        return
    
    # 创建回测器
    backtester = BacktesterV22('config_v21_final.yaml')
    
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
    logger.info("V2.2 回测结果统计")
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
    
    # 保存结果
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = Path('backtest_results')
    output_dir.mkdir(exist_ok=True)
    
    # 保存 JSON
    json_file = output_dir / f'backtest_v22_{timestamp}.json'
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
                'min_return': min_return if all_trades else 0
            }
        }, f, ensure_ascii=False, default=str)
    logger.info(f"结果保存到：{json_file}")
    
    # 保存 CSV
    if all_trades:
        csv_file = output_dir / f'backtest_v22_{timestamp}.csv'
        trades_df = pd.DataFrame(all_trades)
        trades_df.to_csv(csv_file, index=False, encoding='utf-8-sig')
        logger.info(f"交易记录保存到：{csv_file}")
    
    # 显示部分形态示例
    if all_patterns:
        logger.info("\n" + "="*80)
        logger.info("形态示例（前 5 个）：")
        logger.info("="*80)
        for i, pattern in enumerate(all_patterns[:5], 1):
            logger.info(f"\n形态 {i}:")
            logger.info(f"  代码：{pattern['code']}")
            logger.info(f"  下跌：{pattern['drop_start_date']} 到 {pattern['drop_end_date']} ({pattern['drop_change']*100:.2f}%)")
            logger.info(f"  缩量：{pattern['shrink_date']}")
            logger.info(f"  放量：{pattern['surge_date']} ({pattern['surge_close']:.2f})")
            logger.info(f"  回踩：{pattern['retrace_date']} ({pattern['retrace_close']:.2f})")
            logger.info(f"  方案：{pattern['scheme']}")
            logger.info(f"  缩量到放量天数：{pattern['shrink_to_surge_days']}")


if __name__ == '__main__':
    # 运行回测（使用 baostocks_full 完整数据）
    run_backtest_v22(
        period_start='2019-01-01',
        period_end='2026-04-07',
        data_dir='data/backtest/baostocks_full'
    )
