#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门检查 603529 在 V2.2 下的形态
"""

import pandas as pd
from backtester_v22 import BacktesterV22


def check_603529_v22():
    """检查 603529"""
    # 加载数据
    df = pd.read_csv('data/backtest/local_stocks/603529_data.csv')
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    
    print("="*80)
    print("603529 爱玛科技 - V2.2 回测器检测")
    print("="*80)
    print(f"数据范围：{df['date'].min()} 到 {df['date'].max()}")
    print(f"数据条数：{len(df)}")
    
    # 创建回测器
    backtester = BacktesterV22('config_v21_final.yaml')
    
    # 检测形态
    patterns = backtester.check_all_patterns(df, '603529', '2025-01-01', '2026-12-31')
    
    if patterns:
        print(f"\n✅ 检测到 {len(patterns)} 个形态：")
        for i, pattern in enumerate(patterns, 1):
            print(f"\n--- 形态 {i} ---")
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
            print(f"缩量到放量天数：{pattern['shrink_to_surge_days']}")
            
            # 计算缩量到放量的实际天数
            shrink_idx = df[df['date'] == pd.to_datetime(pattern['shrink_date'])].index[0]
            surge_idx = df[df['date'] == pd.to_datetime(pattern['surge_date'])].index[0]
            actual_days = surge_idx - shrink_idx
            print(f"实际缩量到放量天数：{actual_days}")
    else:
        print("\n❌ 未检测到形态")
        
        # 手动检查为什么
        print("\n" + "="*80)
        print("手动检查原因：")
        print("="*80)
        
        # 检查 2026 年 3 月 23 日的放量
        surge_date = pd.to_datetime('2026-03-23')
        surge_idx = df[df['date'] == surge_date].index[0]
        
        print(f"\n2026-03-23 放量日索引：{surge_idx}")
        print(f"收盘价：{df['close'].iloc[surge_idx]}")
        print(f"成交量：{df['volume'].iloc[surge_idx]}")
        
        # 检查之前的缩量
        print("\n缩量检测（放量日前 25 天内）：")
        for i in range(max(0, surge_idx-25), surge_idx):
            vol_i = df['volume'].iloc[i]
            vol_avg_20 = df['volume'].iloc[max(0, i-20):i].mean() if i >= 20 else df['volume'].iloc[0:i].mean()
            if vol_avg_20 > 0:
                ratio = vol_i / vol_avg_20
                if ratio <= 0.6:
                    print(f"  {df['date'].iloc[i].date()}: 成交量 {vol_i}, 均量 {vol_avg_20:.0f}, 比率 {ratio*100:.1f}%")
        
        # 检查大跌
        print("\n大跌检测（20 日内跌幅>12%）：")
        for i in range(20, min(surge_idx, len(df))):
            window_df = df.iloc[i-20:i+1]
            high_price = window_df['high'].max()
            low_price = window_df['low'].min()
            drop = (high_price - low_price) / high_price
            if drop >= 0.12:
                print(f"  {window_df['date'].iloc[0].date()} 到 {window_df['date'].iloc[-1].date()}: 跌幅 {drop*100:.2f}%")


if __name__ == '__main__':
    check_603529_v22()
