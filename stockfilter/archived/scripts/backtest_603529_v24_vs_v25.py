#!/usr/bin/env python3
"""
对比 V2.4 和 V2.5 在 603529 上的回测结果
V2.5 放宽回踩要求到 94%
"""

import pandas as pd
import baostock as bs
from backtester_v24 import BacktesterV24
from backtester_v25_relaxed import BacktesterV25

def backtest_603529_v24_vs_v25():
    print("=" * 100)
    print("📊 603529 爱玛科技 V2.4 vs V2.5 回测对比")
    print("V2.5: 回踩要求从 97% 放宽到 94%")
    print("=" * 100)
    
    # 获取数据
    print("\n1️⃣  获取数据...")
    lg = bs.login()
    rs = bs.query_history_k_data_plus(
        "sh.603529",
        "date,open,high,low,close,volume,amount",
        start_date="2025-08-01",
        end_date="2026-03-30",
        frequency="d",
        adjustflag="2"  # 后复权
    )
    
    data_list = []
    while (rs.error_code == '0') & rs.next():
        data_list.append(rs.get_row_data())
    
    df = pd.DataFrame(data_list, columns=rs.fields)
    bs.logout()
    
    # 数据预处理
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    
    numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    
    print(f"   数据条数：{len(df)}")
    print(f"   日期范围：{df['date'].min().date()} 到 {df['date'].max().date()}")
    
    # V2.4 回测
    print("\n2️⃣  V2.4 回测（回踩要求 97%）")
    print("=" * 100)
    backtester_v24 = BacktesterV24()
    patterns_v24 = backtester_v24.check_all_patterns(
        df=df,
        code="603529",
        period_start="2025-08-25",
        period_end="2026-03-30"
    )
    
    if patterns_v24:
        print(f"   ✅ 检测到 {len(patterns_v24)} 个信号")
        
        # 模拟交易
        trades_v24 = []
        for pattern in patterns_v24:
            trade = backtester_v24.simulate_trade(df, pattern)
            if trade:
                trades_v24.append(trade)
        
        print(f"   完成交易：{len(trades_v24)} 笔")
        
        # 统计收益
        if trades_v24:
            profitable = sum(1 for t in trades_v24 if t['是否盈利'])
            total_return = sum(t['实际收益'] for t in trades_v24)
            avg_return = total_return / len(trades_v24)
            max_return = max(t['实际收益'] for t in trades_v24)
            min_return = min(t['实际收益'] for t in trades_v24)
            
            print(f"   盈利交易：{profitable}/{len(trades_v24)} ({profitable/len(trades_v24)*100:.1f}%)")
            print(f"   总收益：{total_return:.2%}")
            print(f"   平均收益：{avg_return:.2%}")
            print(f"   最大盈利：{max_return:.2%}")
            print(f"   最大亏损：{min_return:.2%}")
            
            # 显示交易详情
            print("\n   交易详情：")
            for i, trade in enumerate(trades_v24, 1):
                print(f"   {i}. {trade['买入日期'].date()}: {trade['买入价格']:.2f} → "
                      f"{trade['退出价格']:.2f} ({trade['实际收益']:.2%})")
    else:
        print(f"   ❌ 未检测到信号")
        trades_v24 = []
    
    # V2.5 回测
    print("\n3️⃣  V2.5 回测（回踩要求 94%）")
    print("=" * 100)
    backtester_v25 = BacktesterV25()
    patterns_v25 = backtester_v25.check_all_patterns(
        df=df,
        code="603529",
        period_start="2025-08-25",
        period_end="2026-03-30"
    )
    
    if patterns_v25:
        print(f"   ✅ 检测到 {len(patterns_v25)} 个信号")
        
        # 模拟交易
        trades_v25 = []
        for pattern in patterns_v25:
            trade = backtester_v25.simulate_trade(df, pattern)
            if trade:
                trades_v25.append(trade)
        
        print(f"   完成交易：{len(trades_v25)} 笔")
        
        # 统计收益
        if trades_v25:
            profitable = sum(1 for t in trades_v25 if t['是否盈利'])
            total_return = sum(t['实际收益'] for t in trades_v25)
            avg_return = total_return / len(trades_v25)
            max_return = max(t['实际收益'] for t in trades_v25)
            min_return = min(t['实际收益'] for t in trades_v25)
            
            print(f"   盈利交易：{profitable}/{len(trades_v25)} ({profitable/len(trades_v25)*100:.1f}%)")
            print(f"   总收益：{total_return:.2%}")
            print(f"   平均收益：{avg_return:.2%}")
            print(f"   最大盈利：{max_return:.2%}")
            print(f"   最大亏损：{min_return:.2%}")
            
            # 显示交易详情
            print("\n   交易详情：")
            for i, trade in enumerate(trades_v25, 1):
                print(f"   {i}. {trade['买入日期'].date()}: {trade['买入价格']:.2f} → "
                      f"{trade['退出价格']:.2f} ({trade['实际收益']:.2%})")
    else:
        print(f"   ❌ 未检测到信号")
        trades_v25 = []
    
    # 对比分析
    print("\n4️⃣  V2.4 vs V2.5 对比")
    print("=" * 100)
    
    if trades_v24 or trades_v25:
        v24_count = len(trades_v24)
        v25_count = len(trades_v25)
        
        v24_total = sum(t['实际收益'] for t in trades_v24) if trades_v24 else 0
        v25_total = sum(t['实际收益'] for t in trades_v25) if trades_v25 else 0
        
        v24_win = sum(1 for t in trades_v24 if t['是否盈利']) if trades_v24 else 0
        v25_win = sum(1 for t in trades_v25 if t['是否盈利']) if trades_v25 else 0
        
        print(f"{'指标':<20} {'V2.4(97%)':<15} {'V2.5(94%)':<15} {'差异':<15}")
        print(f"{'-'*65}")
        print(f"{'信号数量':<20} {v24_count:<15} {v25_count:<15} {v25_count-v24_count:+d}")
        print(f"{'总收益':<20} {v24_total:>10.2%} {v25_total:>10.2%} {(v25_total-v24_total):>10.2%}")
        print(f"{'平均收益':<20} {v24_total/v24_count:>10.2%} {v25_total/v25_count:>10.2%} "
              f"{(v25_total/v25_count if v25_count else 0) - (v24_total/v24_count if v24_count else 0):>10.2%}")
        print(f"{'盈利交易':<20} {v24_win}/{v24_count:<10} {v25_win}/{v25_count:<10} "
              f"{(v25_win-v24_win):+d}")
        
        if v24_count > 0:
            print(f"{'胜率':<20} {v24_win/v24_count:>10.2%} {v25_win/v25_count if v25_count else 0:>10.2%} "
                  f"{(v25_win/v25_count if v25_count else 0) - (v24_win/v24_count):>10.2%}")
        
        print(f"\n{'='*100}")
        
        # 结论
        if v25_count > v24_count:
            print(f"✅ V2.5 放宽回踩要求后，信号数量从 {v24_count} 增加到 {v25_count} (+{v25_count-v24_count})")
        elif v25_count < v24_count:
            print(f"⚠️  V2.5 信号数量反而减少 ({v24_count} → {v25_count})")
        else:
            print(f"➡️  V2.4 和 V2.5 信号数量相同")
        
        if v25_total > v24_total:
            print(f"✅ V2.5 总收益更高 ({v24_total:.2%} → {v25_total:.2%}, +{(v25_total-v24_total):.2%})")
        else:
            print(f"⚠️  V2.5 总收益更低 ({v24_total:.2%} → {v25_total:.2%}, {(v25_total-v24_total):.2%})")
    else:
        print("   两个版本都未检测到信号")
    
    print("=" * 100)

if __name__ == "__main__":
    backtest_603529_v24_vs_v25()
