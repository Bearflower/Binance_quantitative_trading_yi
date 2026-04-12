#!/usr/bin/env python3
"""
多时间框架数据回测执行脚本

用法：
    python run_multi_timeframe_backtest.py --data multi_timeframe_data.json --capital 500 --output backtest_report_v5_0_real.json
"""

import argparse
import json
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, Any

# 添加项目根目录到路径
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.multi_timeframe_backtester import run_backtest

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_multi_timeframe_data(data_file: str) -> Dict[str, Dict[str, list]]:
    """从 JSON 文件加载多时间框架数据"""
    with open(data_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def print_backtest_report(report: Dict[str, Any]):
    """打印回测报告"""
    print("\n" + "=" * 80)
    print("多时间框架数据回测报告")
    print("=" * 80)
    
    # 处理没有交易的情况
    if 'summary' not in report:
        print("\n⚠️  回测未完成或没有交易记录")
        print(f"   消息：{report.get('message', '未知错误')}")
        return
    
    summary = report['summary']
    assessment = report.get('performance_assessment', {})
    
    # 基础信息
    print(f"\n📊 基础统计")
    print(f"  总交易数：{summary.get('total_trades', 0)} 笔")
    print(f"  盈利交易：{summary.get('winning_trades', 0)} 笔")
    print(f"  亏损交易：{summary.get('losing_trades', 0)} 笔")
    print(f"  总手续费：{summary.get('total_fees', 0):.2f}U")
    
    # 盈利能力
    print(f"\n💰 盈利能力")
    print(f"  初始资金：500U")
    print(f"  最终资金：{summary['final_capital']:.2f}U")
    print(f"  总盈亏：{summary['total_pnl']:.2f}U")
    print(f"  总收益率：{summary['total_return']:.1%}")
    print(f"  年化收益：{summary['annualized_return']:.1%}")
    
    # 稳定性
    print(f"\n📈 稳定性")
    print(f"  胜率：{summary['win_rate']:.1%} ({assessment['win_rate']})")
    print(f"  盈亏比：{summary['profit_loss_ratio']:.2f} ({assessment['profit_loss_ratio']})")
    print(f"  夏普比率：{summary['sharpe_ratio']:.2f}")
    
    # 风险
    print(f"\n⚠️  风险指标")
    print(f"  最大回撤：{summary['max_drawdown']:.1%} ({assessment['max_drawdown']})")
    
    # 综合评估
    print(f"\n🏆 综合评估")
    print(f"  等级：{assessment['overall']}")
    
    # 按信号等级统计
    if report.get('grade_statistics'):
        print(f"\n📊 按信号等级统计")
        print(f"  {'等级':<6} {'交易数':<8} {'胜率':<10} {'总盈亏':<12}")
        print(f"  {'-' * 40}")
        for grade in ['S', 'A', 'B']:
            if grade in report['grade_statistics']:
                stats = report['grade_statistics'][grade]
                print(f"  {grade:<6} {stats['trades']:<8} {stats['win_rate']:<10.1%} {stats['total_pnl']:<12.2f}U")
    
    # 交易记录样本
    if report.get('trades'):
        print(f"\n📝 交易记录样本 (前 10 笔)")
        print(f"  {'#':<4} {'币种':<10} {'方向':<6} {'入场价':<12} {'出场价':<12} {'盈亏':<12} {'平仓原因':<15}")
        print(f"  {'-' * 80}")
        for i, trade in enumerate(report['trades'][:10], 1):
            symbol = trade['symbol']
            direction = trade['direction']
            entry_price = f"{float(trade['entry_price']):.2f}"
            exit_price = f"{float(trade['exit_price']):.2f}"
            pnl = f"{float(trade['pnl']):+.2f}U"
            exit_reason = trade['exit_reason']
            print(f"  {i:<4} {symbol:<10} {direction:<6} {entry_price:<12} {exit_price:<12} {pnl:<12} {exit_reason:<15}")
    
    # 优化建议
    print(f"\n💡 优化建议")
    if summary['win_rate'] < Decimal('0.45'):
        print("  ⚠️  胜率偏低，建议优化入场信号")
    if summary['profit_loss_ratio'] < Decimal('1.8'):
        print("  ⚠️  盈亏比偏低，建议优化止盈策略")
    if summary['max_drawdown'] > Decimal('0.15'):
        print("  ⚠️  回撤过大，建议加强风险控制")
    if summary['total_trades'] < 20:
        print("  ℹ️  交易笔数不足，需要更多数据")
    
    print("\n" + "=" * 80)


def main():
    parser = argparse.ArgumentParser(description='多时间框架策略回测')
    parser.add_argument(
        '--data',
        type=str,
        required=True,
        help='多时间框架历史数据 JSON 文件路径'
    )
    parser.add_argument(
        '--capital',
        type=str,
        default='500',
        help='初始资金 (默认：500)'
    )
    parser.add_argument(
        '--output',
        type=str,
        default='backtest_report_v5_0_real.json',
        help='回测报告输出文件名 (默认：backtest_report_v5_0_real.json)'
    )
    
    args = parser.parse_args()
    
    # 加载数据
    logger.info(f"加载多时间框架数据：{args.data}")
    multi_timeframe_data = load_multi_timeframe_data(args.data)
    
    # 打印数据信息
    total_klines = sum(
        len(timeframes[tf])
        for timeframes in multi_timeframe_data.values()
        for tf in timeframes
    )
    logger.info(f"数据加载完成")
    logger.info(f"  币种数：{len(multi_timeframe_data)}")
    logger.info(f"  总 K 线数：{total_klines}")
    
    for symbol, timeframes in multi_timeframe_data.items():
        if '1h' in timeframes and timeframes['1h']:
            first_ts = timeframes['1h'][0]['timestamp']
            last_ts = timeframes['1h'][-1]['timestamp']
            logger.info(f"  {symbol}: 1h 数据 {first_ts} ~ {last_ts}")
        if '4h' in timeframes and timeframes['4h']:
            logger.info(f"    4h: {len(timeframes['4h'])} 条")
        if '1d' in timeframes and timeframes['1d']:
            logger.info(f"    1d: {len(timeframes['1d'])} 条")
    
    # 确定回测时间范围
    all_timestamps = []
    for symbol, timeframes in multi_timeframe_data.items():
        if '1h' in timeframes:
            for kline in timeframes['1h']:
                all_timestamps.append(datetime.fromisoformat(kline['timestamp']))
    
    if not all_timestamps:
        logger.error("没有有效的数据")
        return
    
    start_date = min(all_timestamps)
    end_date = max(all_timestamps)
    
    logger.info(f"回测期间：{start_date} ~ {end_date}")
    
    # 运行回测
    logger.info(f"开始回测，初始资金：{args.capital}U")
    report = run_backtest(
        multi_timeframe_data=multi_timeframe_data,
        start_date=start_date,
        end_date=end_date,
        initial_capital=Decimal(args.capital)
    )
    
    # 打印报告
    print_backtest_report(report)
    
    # 保存报告
    output_path = Path(args.output)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    
    logger.info(f"\n✅ 回测完成！报告已保存到：{output_path}")
    logger.info(f"   文件大小：{output_path.stat().st_size / 1024:.2f} KB")


if __name__ == '__main__':
    main()
