#!/usr/bin/env python3
"""
检查 603529 爱玛科技在 V2.4 版本下是否满足形态
时间范围：2025-08-25 到 2026-03-30
"""

import pandas as pd
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from backtester_v24 import BacktesterV24

def check_603529_v24():
    # 股票数据路径
    stock_code = "603529"
    data_path = project_root / "data" / "backtest" / "baostocks_full" / f"{stock_code}.csv"
    
    if not data_path.exists():
        print(f"❌ 数据文件不存在：{data_path}")
        return
    
    # 读取数据
    df = pd.read_csv(data_path)
    df['date'] = pd.to_datetime(df['date'])
    
    # 筛选时间范围
    start_date = pd.to_datetime("2025-08-25")
    end_date = pd.to_datetime("2026-03-30")
    df_filtered = df[(df['date'] >= start_date) & (df['date'] <= end_date)].copy()
    
    if len(df_filtered) == 0:
        print(f"❌ 在 {start_date.date()} 到 {end_date.date()} 期间没有数据")
        return
    
    print(f"📊 603529 爱玛科技 V2.4 形态检测")
    print(f"时间范围：{start_date.date()} 到 {end_date.date()}")
    print(f"数据条数：{len(df_filtered)}")
    print(f"数据范围：{df_filtered['date'].min().date()} 到 {df_filtered['date'].max().date()}")
    print("=" * 80)
    
    # 创建 V2.4 回测器
    backtester = BacktesterV24()
    
    # 检测形态
    patterns = backtester.check_all_patterns(
        df=df_filtered,
        stock_code=stock_code,
        stock_name="爱玛科技"
    )
    
    if patterns:
        print(f"\n✅ 检测到 {len(patterns)} 个满足 V2.4 形态的信号！\n")
        
        for i, pattern in enumerate(patterns, 1):
            print(f"{'='*80}")
            print(f"信号 {i}:")
            print(f"  股票代码：{pattern['stock_code']}")
            print(f"  股票名称：{pattern['stock_name']}")
            print(f"  大跌日期：{pattern['大跌日期']}")
            print(f"  大跌跌幅：{pattern['大跌跌幅']:.2%}")
            print(f"  缩量日期：{pattern['缩量日期']}")
            print(f"  放量日期：{pattern['放量日期']}")
            print(f"  缩量到放量天数：{pattern['缩量到放量天数']}")
            print(f"  放量涨幅：{pattern['放量涨幅']:.2%}")
            print(f"  放量量比：{pattern['放量量比']:.2f}")
            print(f"  回踩日期：{pattern['回踩日期']}")
            print(f"  回踩确认日期：{pattern['回踩确认日期']}")
            print(f"  建议买入日期：{pattern['建议买入日期']}")
            print(f"  支撑位：{pattern['支撑位']:.2f}")
            print(f"  止损价：{pattern['止损价']:.2f}")
            
            # 获取建议买入日期后的数据
            if pattern['建议买入日期'] and pattern['建议买入日期'] in df_filtered['date'].values:
                buy_idx = df_filtered[df_filtered['date'] == pattern['建议买入日期']].index[0]
                buy_price = df_filtered.loc[buy_idx, 'close']
                print(f"  建议买入价：{buy_price:.2f}")
                
                # 计算后续收益
                future_data = df_filtered.iloc[buy_idx:]
                if len(future_data) > 1:
                    max_price = future_data['high'].max()
                    min_price = future_data['low'].min()
                    max_return = (max_price - buy_price) / buy_price
                    min_return = (min_price - buy_price) / buy_price
                    print(f"  后续最高价：{max_price:.2f} (收益：{max_return:.2%})")
                    print(f"  后续最低价：{min_price:.2f} (收益：{min_return:.2%})")
            
            print(f"{'='*80}\n")
    else:
        print(f"\n❌ 在 {start_date.date()} 到 {end_date.date()} 期间未检测到满足 V2.4 形态的信号\n")
        
        # 分析原因
        print("=" * 80)
        print("可能的原因分析：")
        print("=" * 80)
        
        # 检查是否有大跌
        df_sorted = df_filtered.sort_values('date')
        if len(df_sorted) > 30:
            # 计算 30 日跌幅
            df_sorted = df_sorted.copy()
            df_sorted['max_close_30'] = df_sorted['close'].rolling(window=30, min_periods=1).max()
            df_sorted['drop_from_max'] = (df_sorted['max_close_30'] - df_sorted['close']) / df_sorted['max_close_30']
            
            max_drop = df_sorted['drop_from_max'].max()
            print(f"1. 期间最大跌幅：{max_drop:.2%} (V2.4 要求：≥8%)")
            
            if max_drop < 0.08:
                print(f"   ❌ 跌幅不足，这是主要原因\n")
            else:
                print(f"   ✅ 跌幅满足要求\n")
        
        # 检查成交量变化
        if len(df_sorted) > 20:
            df_sorted = df_sorted.copy()
            df_sorted['avg_volume_20'] = df_sorted['volume'].rolling(window=20, min_periods=1).mean()
            df_sorted['volume_ratio'] = df_sorted['volume'] / df_sorted['avg_volume_20']
            
            min_volume_ratio = df_sorted['volume_ratio'].min()
            max_volume_ratio = df_sorted['volume_ratio'].max()
            print(f"2. 量比范围：{min_volume_ratio:.2f} - {max_volume_ratio:.2f} (V2.4 要求：1.2-15)")
            
            if max_volume_ratio < 1.2:
                print(f"   ❌ 成交量未明显放大\n")
            else:
                print(f"   ✅ 成交量有放大\n")
        
        # 检查是否有放量上涨
        if len(df_sorted) > 20:
            surge_days = df_sorted[(df_sorted['close'] / df_sorted['close'].shift(1) - 1 >= 0.03) & 
                                   (df_sorted['volume_ratio'] >= 1.2)]
            print(f"3. 放量上涨天数（涨幅≥3% 且量比≥1.2）：{len(surge_days)}")
            
            if len(surge_days) == 0:
                print(f"   ❌ 没有明显的放量上涨\n")
            else:
                print(f"   ✅ 有放量上涨\n")
        
        print("=" * 80)
        print("\n💡 建议：")
        print("如果确实不满足，可能是以下原因：")
        print("1. 时间跨度不够（V2.4 要求缩量到放量 60 天内）")
        print("2. 跌幅不够（V2.4 要求≥8%）")
        print("3. 涨幅不够（V2.4 要求≥3%）")
        print("4. 量比不够（V2.4 要求 1.2-15 倍）")
        print("5. 回踩确认未完成")
        print("=" * 80)

if __name__ == "__main__":
    check_603529_v24()
