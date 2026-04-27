#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
详细检查 603529 爱玛科技在 2026 年 3 月 23 日放量后的形态
"""

import pandas as pd
from backtester_scheme_ab import BacktesterWithRules_AB


def check_pattern_detailed():
    """详细检查形态"""
    # 读取本地数据
    df = pd.read_csv('data/backtest/local_stocks/603529_data.csv')
    df['date'] = pd.to_datetime(df['date'])
    
    print("="*80)
    print("603529 爱玛科技 - 2026 年 3 月 23 日放量上涨详细分析")
    print("="*80)
    
    # 创建回测器
    backtester = BacktesterWithRules_AB('config_v21_final.yaml')
    
    # 检查 3 月 23 日前后的数据
    print("\n3 月 23 日前后 K 线数据：")
    print(df[(df['date'] >= '2026-02-01') & (df['date'] <= '2026-03-31')].to_string(index=False))
    
    # 手动检测形态
    print("\n" + "="*80)
    print("手动形态检测：")
    print("="*80)
    
    # 1. 检测大跌
    print("\n1. 大跌检测（20 日内跌幅>12%）:")
    for i in range(20, len(df)):
        window_df = df.iloc[i-20:i+1]
        high_price = window_df['high'].max()
        low_price = window_df['low'].min()
        drop = (high_price - low_price) / high_price
        
        if drop >= 0.12:
            drop_start_date = window_df[window_df['high'] == high_price]['date'].iloc[0]
            drop_end_date = window_df[window_df['low'] == low_price]['date'].iloc[0]
            print(f"   {drop_start_date.date()} 到 {drop_end_date.date()}: 跌幅 {drop*100:.2f}%")
    
    # 2. 检测缩量
    print("\n2. 缩量检测（成交量<20 日均量 60%）:")
    for i in range(20, len(df)):
        vol_i = df['volume'].iloc[i]
        vol_avg_20 = df['volume'].iloc[max(0, i-20):i].mean()
        if vol_avg_20 > 0:
            ratio = vol_i / vol_avg_20
            if ratio <= 0.6:
                print(f"   {df['date'].iloc[i].date()}: 成交量 {vol_i}, 均量 {vol_avg_20:.0f}, 比率 {ratio*100:.1f}%")
    
    # 3. 检测放量
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
                print(f"   {df['date'].iloc[i].date()}: 涨幅 {price_change*100:.2f}%, 量比 {vol_ratio:.2f}, 收盘价 {close_j:.2f}")
    
    # 重点分析 3 月 23 日
    print("\n" + "="*80)
    print("重点分析 2026-03-23 放量启动日：")
    print("="*80)
    
    surge_date = pd.to_datetime('2026-03-23')
    surge_idx_list = df[df['date'] == surge_date].index
    
    if len(surge_idx_list) > 0:
        surge_idx = surge_idx_list[0]
        
        surge_close = df['close'].iloc[surge_idx]
        print(f"\n放量日信息：")
        print(f"  日期：{df['date'].iloc[surge_idx].date()}")
        print(f"  收盘价：{surge_close:.2f}")
        print(f"  成交量：{df['volume'].iloc[surge_idx]}")
        print(f"  成交额：{df['amount'].iloc[surge_idx]}")
        
        # 检查启动后 5 天
        print(f"\n启动后 5 天观察期：")
        support_level = surge_close * 0.97
        print(f"  支撑位：{support_level:.2f} (={surge_close:.2f} × 0.97)")
        
        for i in range(surge_idx+1, min(surge_idx+6, len(df))):
            low_k = df['low'].iloc[i]
            print(f"  {df['date'].iloc[i].date()}: 最低价 {low_k:.2f}, {'✅ 未跌破' if low_k >= support_level else '❌ 跌破'}支撑位")
        
        # 检查回踩
        print(f"\n回踩检测：")
        for i in range(surge_idx+1, min(surge_idx+10, len(df))):
            low_i = df['low'].iloc[i]
            close_i = df['close'].iloc[i]
            
            # 回踩不破支撑位
            if low_i >= support_level * 0.98:  # 允许 2% 的误差
                print(f"  ✅ {df['date'].iloc[i].date()}: 回踩确认！最低价 {low_i:.2f}, 收盘价 {close_i:.2f}")
                break
            else:
                print(f"  ❌ {df['date'].iloc[i].date()}: 最低价 {low_i:.2f} < 支撑位 {support_level:.2f}")
    
    # 检查流动性
    print("\n" + "="*80)
    print("流动性检测（20 日均成交额≥3000 万）:")
    print("="*80)
    
    for i in range(20, len(df)):
        start_idx = i - 20
        recent_df = df.iloc[start_idx:i+1]
        avg_amount = recent_df['amount'].mean()
        
        if avg_amount >= 30_000_000:
            print(f"  {df['date'].iloc[i].date()}: 20 日均成交额 {avg_amount/10000:.0f}万 ✅")
        else:
            print(f"  {df['date'].iloc[i].date()}: 20 日均成交额 {avg_amount/10000:.0f}万 ❌")


if __name__ == '__main__':
    check_pattern_detailed()
