#!/usr/bin/env python3
"""
做空策略回测执行脚本
完整流程：获取数据 -> 运行回测 -> 生成报告
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.short_selling_backtester import ShortSellingBacktester, run_short_backtest
from backtesting.performance_analyzer import PerformanceAnalyzer
from backtesting.report_generator import ReportGenerator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='做空策略回测系统')
    parser.add_argument('--data', type=str, default='data/backtest_data.json',
                       help='回测数据文件路径')
    parser.add_argument('--capital', type=str, default='500',
                       help='初始资金 (USDT)')
    parser.add_argument('--days', type=int, default=180,
                       help='回测天数')
    parser.add_argument('--output', type=str, default='data/backtest_report',
                       help='输出报告路径 (不含扩展名)')
    parser.add_argument('--fetch-data', action='store_true',
                       help='是否先获取最新数据')
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("做空策略回测系统 v1.0")
    print("=" * 80)
    
    if args.fetch_data:
        print("\n📥 正在获取最新数据...")
        from scripts.fetch_backtest_data import fetch_multi_timeframe_data, TARGET_SYMBOLS
        
        data = fetch_multi_timeframe_data(TARGET_SYMBOLS, days=args.days)
        
        data_path = Path(args.data)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(data_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"✅ 数据已保存到：{data_path}")
    else:
        data_path = Path(args.data)
        if not data_path.exists():
            logger.error(f"❌ 数据文件不存在：{data_path}")
            logger.error("请使用 --fetch-data 参数先获取数据")
            sys.exit(1)
    
    logger.info(f"加载回测数据：{data_path}")
    with open(data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    symbols = list(data.keys())
    logger.info(f"加载了 {len(symbols)} 个币种的数据")
    
    all_timestamps = []
    for symbol, symbol_data in data.items():
        for k in symbol_data.get('1h', []):
            try:
                ts = datetime.fromisoformat(k['timestamp'])
                all_timestamps.append(ts)
            except:
                pass
    
    if not all_timestamps:
        logger.error("❌ 没有有效的 K 线数据")
        sys.exit(1)
    
    end_date = max(all_timestamps)
    start_date = end_date - timedelta(days=args.days)
    
    logger.info(f"回测期间：{start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
    logger.info(f"初始资金：{args.capital} USDT")
    
    print("\n" + "=" * 80)
    print("开始运行回测...")
    print("=" * 80)
    
    report = run_short_backtest(
        data_path=str(data_path),
        start_date=start_date,
        end_date=end_date,
        capital=Decimal(args.capital)
    )
    
    print("\n" + "=" * 80)
    print("回测完成！生成分析报告...")
    print("=" * 80)
    
    analyzer = PerformanceAnalyzer(report)
    analysis = analyzer.full_analysis()
    
    output_json = f"{args.output}.json"
    output_md = f"{args.output}.md"
    
    generator = ReportGenerator(report, analysis)
    result = generator.generate_all(output_json, output_md)
    
    logger.info(f"✅ JSON 报告：{result['json_report']}")
    logger.info(f"✅ Markdown 报告：{result['markdown_report']}")
    
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
    print(f"  综合评分：{assessment.get('score', 0):.1f}/5.0")
    
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
