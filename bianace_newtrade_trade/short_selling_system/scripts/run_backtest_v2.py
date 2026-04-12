#!/usr/bin/env python3
"""
运行优化版回测 v2
支持 15 分钟和 5 分钟 K 线对比
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.short_selling_backtester_v2 import run_backtest_v2
from backtesting.performance_analyzer import PerformanceAnalyzer
from backtesting.report_generator import ReportGenerator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='运行优化版回测 v2')
    parser.add_argument('--data', type=str, default='data/backtest_data.json')
    parser.add_argument('--capital', type=str, default='500')
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--timeframe', type=str, default='15m', choices=['15m', '5m'])
    parser.add_argument('--output', type=str, default='data/backtest_v2')
    parser.add_argument('--no-filter', action='store_true', help='禁用币种筛选')
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("优化版回测 v2 - ATR 动态止损 + 分批止盈 + 移动止盈")
    print("=" * 80)
    print(f"时间框架：{args.timeframe}")
    print(f"数据文件：{args.data}")
    print(f"初始资金：{args.capital} USDT")
    print(f"回测天数：{args.days} 天")
    print(f"币种筛选：{'禁用' if args.no_filter else '启用'}")
    print("=" * 80)
    
    # 加载数据
    logger.info(f"加载回测数据：{args.data}")
    with open(args.data, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 确定回测时间范围
    all_timestamps = []
    for symbol, symbol_data in data.items():
        for k in symbol_data.get(args.timeframe, []):
            try:
                ts = datetime.fromisoformat(k['timestamp'])
                all_timestamps.append(ts)
            except:
                pass
    
    if not all_timestamps:
        logger.error("❌ 没有有效的 K 线数据")
        return
    
    end_date = max(all_timestamps)
    start_date = end_date - timedelta(days=args.days)
    
    logger.info(f"回测期间：{start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
    
    # 运行回测
    report = run_backtest_v2(
        data_path=args.data,
        start_date=start_date,
        end_date=end_date,
        capital=Decimal(args.capital),
        timeframe=args.timeframe,
        use_filter=not args.no_filter
    )
    
    # 生成报告
    analyzer = PerformanceAnalyzer(report)
    analysis = analyzer.full_analysis()
    
    output_json = f"{args.output}_{args.timeframe}.json"
    output_md = f"{args.output}_{args.timeframe}.md"
    
    generator = ReportGenerator(report, analysis)
    result = generator.generate_all(output_json, output_md)
    
    logger.info(f"✅ JSON 报告：{result['json_report']}")
    logger.info(f"✅ Markdown 报告：{result['markdown_report']}")
    
    # 打印摘要
    print("\n" + "=" * 80)
    print("回测结果摘要")
    print("=" * 80)
    
    summary = report.get('summary', {})
    assessment = analysis.get('performance_assessment', {})
    
    print(f"\n📊 基础统计")
    print(f"  总交易：{summary.get('total_trades', 0)} 笔")
    print(f"  盈利：{summary.get('winning_trades', 0)} 笔 | 亏损：{summary.get('losing_trades', 0)} 笔")
    
    print(f"\n💰 盈利能力")
    print(f"  初始：{summary.get('initial_capital', 0):.0f}U → 最终：{summary.get('final_capital', 0):.2f}U")
    print(f"  总盈亏：{summary.get('total_pnl', 0):.2f}U")
    print(f"  收益率：{summary.get('total_return', 0):.1%}")
    
    print(f"\n📈 稳定性")
    print(f"  胜率：{summary.get('win_rate', 0):.1%} ({assessment.get('win_rate', 'N/A')})")
    print(f"  盈亏比：{summary.get('profit_loss_ratio', 0):.2f} ({assessment.get('profit_loss_ratio', 'N/A')})")
    
    print(f"\n🏆 综合评估：{assessment.get('overall', 'N/A')}")
    
    exit_reasons = report.get('exit_reason_stats', {})
    if exit_reasons:
        print(f"\n🚪 出场原因")
        for reason, count in exit_reasons.items():
            pct = count / summary.get('total_trades', 1) * 100
            print(f"  {reason}: {count} 笔 ({pct:.1f}%)")
    
    symbol_stats = report.get('symbol_stats', {})
    if symbol_stats:
        print(f"\n🪙 币种表现 Top 3")
        sorted_symbols = sorted(symbol_stats.items(), key=lambda x: x[1]['total_pnl'], reverse=True)
        for symbol, stats in sorted_symbols[:3]:
            win_rate = stats['wins'] / stats['trades'] if stats['trades'] > 0 else 0
            print(f"  {symbol}: {stats['trades']}笔，胜率{win_rate:.1%}, 盈亏{stats['total_pnl']:.2f}U")
    
    recommendations = analysis.get('recommendations', [])
    if recommendations:
        print(f"\n💡 优化建议")
        for rec in recommendations[:3]:
            print(f"  {rec}")
    
    print("\n" + "=" * 80)
    print("✅ 回测完成！")
    print("=" * 80)


if __name__ == '__main__':
    main()
