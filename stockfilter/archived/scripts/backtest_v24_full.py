#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.4 全量回测脚本
- 使用 baostocks_full 完整数据（3317 只股票）
- 目标：验证 V2.4 参数是否能在 2020 年后产生足够信号
"""

import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict
import os

from backtester_v24 import BacktesterV24
from utils.logger import get_logger

logger = get_logger()


def load_all_stock_data(data_dir: str = 'data/backtest/baostocks_full') -> List[Dict]:
    """加载所有本地股票数据"""
    stock_data = []
    
    if not os.path.exists(data_dir):
        logger.error(f"数据目录不存在：{data_dir}")
        return stock_data
    
    files = sorted([f for f in os.listdir(data_dir) if f.endswith('_data.csv')])
    
    for i, file in enumerate(files):
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
            if (i+1) % 100 == 0:
                logger.info(f"已加载 {i+1} 只股票...")
        except Exception as e:
            logger.error(f"加载 {code} 数据失败：{e}")
    
    return stock_data


def run_backtest_v24_full():
    """运行 V2.4 全量回测"""
    logger.info("="*80)
    logger.info("开始 V2.4 全量回测（3317 只股票）")
    logger.info("="*80)
    logger.info("V2.4 参数设置（激进放宽）：")
    logger.info("  - 缩量到放量时间窗口：60 天")
    logger.info("  - 跌幅阈值：8%")
    logger.info("  - 放量涨幅：3%")
    logger.info("  - 量比最小值：1.2")
    logger.info("  - 量比最大值：15")
    logger.info("  - 缩量比率：80%")
    logger.info("  - 流动性要求：2000 万")
    logger.info("目标：验证 2020 年后信号数量")
    logger.info("="*80)
    
    # 加载数据
    logger.info("加载 baostocks_full 完整数据...")
    stock_data = load_all_stock_data('data/backtest/baostocks_full')
    logger.info(f"共加载 {len(stock_data)} 只股票数据")
    
    if not stock_data:
        logger.error("没有加载到任何股票数据")
        return
    
    # 创建回测器
    backtester = BacktesterV24('config_v21_final.yaml')
    
    # 回测结果
    all_patterns = []
    all_trades = []
    
    # 遍历所有股票
    for i, stock in enumerate(stock_data):
        code = stock['code']
        df = stock['data']
        
        # 检测所有形态
        patterns = backtester.check_all_patterns(df, code, '2019-01-01', '2026-04-07')
        
        if patterns:
            if len(patterns) > 1:
                logger.info(f"{code}: 检测到 {len(patterns)} 个形态")
            all_patterns.extend(patterns)
            
            # 对每个形态模拟交易
            for pattern in patterns:
                trade = backtester.simulate_trade(df, pattern)
                if trade:
                    all_trades.append(trade)
        
        if (i+1) % 500 == 0:
            logger.info(f"进度：{i+1}/{len(stock_data)}，已检测到 {len(all_patterns)} 个形态")
    
    # 统计结果
    logger.info("="*80)
    logger.info("V2.4 全量回测结果统计")
    logger.info("="*80)
    
    total_stocks = len(stock_data)
    matched_stocks = len(set(p['code'] for p in all_patterns))
    total_patterns = len(all_patterns)
    total_trades = len(all_trades)
    
    logger.info(f"检测股票数：{total_stocks}")
    logger.info(f"满足形态股票数：{matched_stocks}")
    logger.info(f"总形态数：{total_patterns}")
    logger.info(f"总交易数：{total_trades}")
    
    # 按年份统计
    def get_year_from_date(date_obj):
        if hasattr(date_obj, 'year'):
            return str(date_obj.year)
        else:
            return str(date_obj)[:4]
    
    yearly_stats = {}
    for pattern in all_patterns:
        year = get_year_from_date(pattern['retrace_date'])
        yearly_stats[year] = yearly_stats.get(year, 0) + 1
    
    logger.info("\n按年份统计：")
    for year in sorted(yearly_stats.keys()):
        logger.info(f"{year}年：{yearly_stats[year]} 个形态")
    
    # 排除 2019 年后的统计
    patterns_exclude_2019 = [p for p in all_patterns if get_year_from_date(p['retrace_date']) != '2019']
    logger.info(f"\n排除 2019 年后形态数：{len(patterns_exclude_2019)}")
    
    # 计算每月平均信号数（排除 2019）
    if patterns_exclude_2019:
        dates = [p['retrace_date'] for p in patterns_exclude_2019]
        min_date = min(dates)
        max_date = max(dates)
        
        min_dt = pd.to_datetime(min_date)
        max_dt = pd.to_datetime(max_date)
        months_diff = (max_dt.year - min_dt.year) * 12 + (max_dt.month - min_dt.month) + 1
        
        avg_per_month = len(patterns_exclude_2019) / months_diff
        logger.info(f"\n数据时间范围：{min_date} 到 {max_date}")
        logger.info(f"月份数：{months_diff} 个月")
        logger.info(f"平均每月信号数：{avg_per_month:.2f} 个/月")
        logger.info(f"平均每年信号数：{avg_per_month * 12:.2f} 个/年")
        
        # 检查是否达到目标
        if avg_per_month >= 2:
            logger.info(f"✅ 达到目标：每月 {avg_per_month:.2f} 个信号（目标：2-3 个）")
        else:
            logger.info(f"⚠️  未达到目标：每月 {avg_per_month:.2f} 个信号（目标：2-3 个）")
    
    # 计算收益统计（排除 2019）
    if all_trades:
        trades_exclude_2019 = [t for t in all_trades if get_year_from_date(t['buy_date']) != '2019']
        if trades_exclude_2019:
            net_returns = [t['net_return'] for t in trades_exclude_2019]
            avg_return = sum(net_returns) / len(net_returns) * 100
            win_count = sum(1 for r in net_returns if r > 0)
            win_rate = win_count / len(net_returns) * 100
            
            logger.info(f"\n排除 2019 年后的收益统计：")
            logger.info(f"平均收益：{avg_return:.2f}%")
            logger.info(f"胜率：{win_rate:.2f}%")
    
    # 保存结果
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir = Path('backtest_results')
    output_dir.mkdir(exist_ok=True)
    
    # 保存 JSON
    json_file = output_dir / f'backtest_v24_full_{timestamp}.json'
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump({
            'patterns': all_patterns,
            'trades': all_trades,
            'summary': {
                'total_stocks': total_stocks,
                'matched_stocks': matched_stocks,
                'total_patterns': total_patterns,
                'total_trades': total_trades,
                'patterns_exclude_2019': len(patterns_exclude_2019),
                'yearly_stats': yearly_stats
            }
        }, f, ensure_ascii=False, default=str)
    logger.info(f"\n结果保存到：{json_file}")
    
    # 保存 CSV
    if all_trades:
        csv_file = output_dir / f'backtest_v24_full_{timestamp}.csv'
        trades_df = pd.DataFrame(all_trades)
        trades_df.to_csv(csv_file, index=False, encoding='utf-8-sig')
        logger.info(f"交易记录保存到：{csv_file}")
    
    logger.info("\n" + "="*80)
    logger.info("V2.4 全量回测完成！")
    logger.info("="*80)


if __name__ == '__main__':
    run_backtest_v24_full()
