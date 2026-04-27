#!/usr/bin/env python3
"""
V2.5 全量回测脚本
放宽回踩要求到 94%，在全市场数据上进行回测
"""

import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from backtester_v25_relaxed import BacktesterV25

def backtest_v25_full():
    print("=" * 100)
    print("📊 V2.5 全量回测（回踩要求 94%）")
    print("=" * 100)
    
    # 数据目录
    data_dir = Path("data/backtest/baostocks_full")
    
    if not data_dir.exists():
        print(f"❌ 数据目录不存在：{data_dir}")
        return
    
    # 获取所有股票文件
    stock_files = list(data_dir.glob("*.csv"))
    print(f"\n找到 {len(stock_files)} 只股票")
    
    # 创建回测器
    backtester = BacktesterV25()
    
    # 回测结果
    all_patterns = []
    all_trades = []
    
    # 遍历所有股票
    for i, stock_file in enumerate(stock_files, 1):
        stock_code = stock_file.stem
        print(f"\r[{i}/{len(stock_files)}] 正在回测 {stock_code}...", end="", flush=True)
        
        try:
            # 读取数据
            df = pd.read_csv(stock_file)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date')
            
            numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # 检测形态
            patterns = backtester.check_all_patterns(
                df=df,
                code=stock_code,
                period_start="2019-01-01",
                period_end="2026-04-14"
            )
            
            if patterns:
                for pattern in patterns:
                    pattern['stock_name'] = stock_code
                    all_patterns.append(pattern)
                    
                    # 模拟交易
                    trade = backtester.simulate_trade(df, pattern)
                    if trade:
                        trade['stock_code'] = stock_code
                        all_trades.append(trade)
        
        except Exception as e:
            print(f"\n❌ {stock_code} 回测失败：{e}")
            continue
    
    print("\n")
    
    # 汇总结果
    print("=" * 100)
    print("📊 V2.5 回测结果汇总")
    print("=" * 100)
    
    print(f"\n✅ 检测到 {len(all_patterns)} 个形态信号")
    print(f"✅ 完成 {len(all_trades)} 笔交易")
    
    # 年度分布
    if all_patterns:
        print("\n📅 年度分布：")
        year_counts = {}
        for pattern in all_patterns:
            year = pattern['放量日期'].year
            year_counts[year] = year_counts.get(year, 0) + 1
        
        for year in sorted(year_counts.keys()):
            print(f"   {year}年：{year_counts[year]} 个")
        
        # 排除 2019 年
        non_2019 = sum(count for year, count in year_counts.items() if year != 2019)
        months = 12 * (max(year_counts.keys()) - 2019)
        print(f"\n   排除 2019 年后：{non_2019} 个")
        print(f"   月均信号：{non_2019 / months:.2f} 个/月")
    
    # 交易统计
    if all_trades:
        print("\n💰 交易统计：")
        profitable = sum(1 for t in all_trades if t['是否盈利'])
        print(f"   盈利交易：{profitable}/{len(all_trades)} ({profitable/len(all_trades)*100:.1f}%)")
        
        total_return = sum(t['实际收益'] for t in all_trades)
        avg_return = total_return / len(all_trades)
        max_return = max(t['实际收益'] for t in all_trades)
        min_return = min(t['实际收益'] for t in all_trades)
        
        print(f"   总收益：{total_return:.2%}")
        print(f"   平均收益：{avg_return:.2%}")
        print(f"   最大盈利：{max_return:.2%}")
        print(f"   最大亏损：{min_return:.2%}")
    
    # 保存结果
    output_dir = Path("backtest_results")
    output_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"backtest_v25_full_{timestamp}.json"
    
    # 序列化数据
    serializable_patterns = []
    for p in all_patterns:
        sp = p.copy()
        for key in ['大跌日期', '缩量日期', '放量日期', '回踩日期', '回踩确认日期', '建议买入日期']:
            if key in sp and pd.notna(sp[key]):
                sp[key] = sp[key].strftime("%Y-%m-%d")
        serializable_patterns.append(sp)
    
    serializable_trades = []
    for t in all_trades:
        st = t.copy()
        if '买入日期' in st and pd.notna(st['买入日期']):
            st['买入日期'] = st['买入日期'].strftime("%Y-%m-%d")
        if '退出日期' in st and pd.notna(st['退出日期']):
            st['退出日期'] = st['退出日期'].strftime("%Y-%m-%d")
        serializable_trades.append(st)
    
    result = {
        'version': 'V2.5',
        'backtest_date': timestamp,
        'parameters': {
            'drop_threshold': 0.08,
            'surge_price_ratio': 0.03,
            'min_volume_ratio': 1.2,
            'max_volume_ratio': 15.0,
            'shrink_to_surge_days': 60,
            'post_surge_max_drop': 0.94,  # V2.5 关键参数
        },
        'patterns': serializable_patterns,
        'trades': serializable_trades,
        'summary': {
            'total_patterns': len(all_patterns),
            'total_trades': len(all_trades),
            'profitable_trades': profitable if all_trades else 0,
            'win_rate': profitable / len(all_trades) if all_trades else 0,
            'total_return': total_return if all_trades else 0,
            'avg_return': avg_return if all_trades else 0,
        }
    }
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ 结果已保存到：{output_file}")
    print("=" * 100)

if __name__ == "__main__":
    backtest_v25_full()
