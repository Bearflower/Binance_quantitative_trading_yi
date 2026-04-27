#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检查 603529 爱玛科技在 2025 年 8 月 25 日到 2026 年 3 月 20 日是否满足 v2.1 形态
"""

import pandas as pd
import akshare as ak
from backtester_scheme_ab import BacktesterWithRules_AB
from utils.logger import get_logger

logger = get_logger()


def get_stock_data(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取股票 K 线数据"""
    print(f"正在获取 {code} 的 K 线数据...")
    
    # 优先使用本地数据文件
    import os
    local_file = f'data/backtest/local_stocks/{code}_data.csv'
    
    if os.path.exists(local_file):
        print(f"使用本地数据文件：{local_file}")
        df = pd.read_csv(local_file)
        df['date'] = pd.to_datetime(df['date'])
        # 筛选日期范围
        df = df[(df['date'] >= pd.to_datetime(start_date)) & (df['date'] <= pd.to_datetime(end_date))]
        print(f"筛选后获取到 {len(df)} 条数据")
        print(f"数据范围：{df['date'].min()} 到 {df['date'].max()}")
        return df
    
    # 如果本地没有，使用 AKShare 获取日线数据
    df = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date.replace('-', ''),
        end_date=end_date.replace('-', ''),
        adjust="qfq"  # 前复权
    )
    
    # 重命名列以匹配回测器要求
    df = df.rename(columns={
        '日期': 'date',
        '开盘': 'open',
        '最高': 'high',
        '最低': 'low',
        '收盘': 'close',
        '成交量': 'volume',
        '成交额': 'amount',
        '振幅': 'amplitude',
        '涨跌幅': 'pct_chg',
        '涨跌额': 'change',
        '换手率': 'turnover'
    })
    
    print(f"获取到 {len(df)} 条数据")
    print(f"数据范围：{df['date'].min()} 到 {df['date'].max()}")
    
    return df


def check_pattern(code: str, start_date: str, end_date: str):
    """检查形态"""
    # 获取数据（获取更长时间范围以确保有足够数据计算指标）
    df = get_stock_data(code, '20250101', '20260331')
    
    if len(df) < 30:
        print(f"数据不足 30 天，无法检测")
        return
    
    # 创建回测器
    backtester = BacktesterWithRules_AB('config_v21_final.yaml')
    
    print("\n" + "="*80)
    print(f"检查 {code} 在 {start_date} 到 {end_date} 期间的形态")
    print("="*80)
    
    # 检测所有形态
    all_patterns = backtester.check_all_patterns(df, code, start_date, end_date)
    
    if len(all_patterns) == 0:
        print("\n❌ 未检测到符合条件的形态")
        print("\n可能的原因：")
        print("1. 跌幅不足 12%")
        print("2. 缩量不充分（成交量未低于 20 日均量的 60%）")
        print("3. 放量启动不符合条件（涨幅<5% 或量比不在 1.5-12 倍之间）")
        print("4. 启动后 5 天内跌破支撑（最低价低于启动价的 97%）")
        print("5. 回踩跌破支撑位")
        print("6. 流动性不足（20 日均成交额<3000 万）")
    else:
        print(f"\n✅ 检测到 {len(all_patterns)} 个符合条件的形态：\n")
        
        for i, pattern in enumerate(all_patterns, 1):
            print(f"--- 形态 {i} ---")
            print(f"股票代码：{pattern['code']}")
            print(f"下跌开始日：{pattern['drop_start_date']}")
            print(f"下跌结束日：{pattern['drop_end_date']}")
            print(f"累计跌幅：{pattern['drop_change']*100:.2f}%")
            print(f"缩量日期：{pattern['shrink_date']}")
            print(f"放量启动日：{pattern['surge_date']}")
            print(f"放量日收盘价：{pattern['surge_close']:.2f}")
            print(f"回踩确认日：{pattern['retrace_date']}")
            print(f"回踩日收盘价：{pattern['retrace_close']:.2f}")
            print(f"回踩日最低价：{pattern['retrace_low']:.2f}")
            print(f"支撑位：{pattern['support_level']:.2f}")
            print(f"适用方案：{pattern['scheme']}")
            print()
    
    # 显示近期 K 线数据，帮助分析
    print("\n" + "="*80)
    print("近期 K 线数据（最后 30 个交易日）：")
    print("="*80)
    print(df.tail(30).to_string(index=False, columns=['date', 'open', 'high', 'low', 'close', 'volume', 'amount']))
    
    # 计算一些技术指标辅助分析
    print("\n" + "="*80)
    print("技术指标分析：")
    print("="*80)
    
    df['ma20'] = df['close'].rolling(20).mean()
    df['vol_ma20'] = df['volume'].rolling(20).mean()
    df['pct_chg'] = df['close'].pct_change() * 100
    
    # 检查是否有大跌
    print("\n1. 大跌检测（20 日内跌幅>12%）:")
    for i in range(20, len(df)):
        window_df = df.iloc[i-20:i+1]
        high_price = window_df['high'].max()
        low_price = window_df['low'].min()
        drop = (high_price - low_price) / high_price
        if drop >= 0.12:
            print(f"   {window_df['date'].iloc[0]} 到 {window_df['date'].iloc[-1]}: 跌幅 {drop*100:.2f}%")
    
    # 检查缩量
    print("\n2. 缩量检测（成交量<20 日均量 60%）:")
    for i in range(20, len(df)):
        vol_i = df['volume'].iloc[i]
        vol_avg_20 = df['volume'].iloc[max(0, i-20):i].mean()
        if vol_avg_20 > 0:
            ratio = vol_i / vol_avg_20
            if ratio <= 0.6:
                print(f"   {df['date'].iloc[i]}: 成交量 {vol_i}, 均量 {vol_avg_20:.0f}, 比率 {ratio*100:.1f}%")
    
    # 检查放量
    print("\n3. 放量检测（涨幅>5%, 量比 1.5-12 倍）:")
    for i in range(1, len(df)):
        vol_j = df['volume'].iloc[i]
        vol_prev = df['volume'].iloc[i-1]
        close_j = df['close'].iloc[i]
        close_prev = df['close'].iloc[i-1]
        
        if vol_prev > 0:
            vol_ratio = vol_j / vol_prev
            price_change = (close_j - close_prev) / close_prev
            
            if price_change >= 0.05 and 1.5 <= vol_ratio <= 12.0:
                print(f"   {df['date'].iloc[i]}: 涨幅 {price_change*100:.2f}%, 量比 {vol_ratio:.2f}")


if __name__ == '__main__':
    check_pattern('603529', '2025-08-25', '2026-03-30')
