#!/usr/bin/env python3
"""
v5.5 平衡优化版回测执行脚本

v5.5 核心改进：
1. 简化评分系统（4 维度 100 分制）
2. 3.0×ATR 止损，5×/7×ATR 止盈
3. 回调入场 + 确认信号
4. 移动止损混合模式
5. 市场状态自适应
"""

import argparse
import json
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.multi_timeframe_backtester_v55_full import run_backtest_v55_full

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='v5.5 平衡优化版多时间框架回测')
    parser.add_argument('--data', type=str, required=True)
    parser.add_argument('--capital', type=str, default='500')
    parser.add_argument('--output', type=str, default='backtest_report_v5_5_full.json')
    
    args = parser.parse_args()
    
    # 加载数据
    logger.info(f"加载数据：{args.data}")
    with open(args.data, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 数据统计
    total_1h = sum(len(tf['1h']) for tf in data.values())
    total_4h = sum(len(tf['4h']) for tf in data.values())
    total_1d = sum(len(tf['1d']) for tf in data.values())
    
    logger.info(f"数据量：")
    logger.info(f"  1h K 线：{total_1h} 条")
    logger.info(f"  4h K 线：{total_4h} 条")
    logger.info(f"  1d K 线：{total_1d} 条")
    
    # 确定回测时间范围
    all_ts = []
    for symbol, tf_data in data.items():
        for k in tf_data.get('1h', []):
            all_ts.append(datetime.fromisoformat(k['timestamp']))
    
    start_date = min(all_ts)
    end_date = max(all_ts)
    
    logger.info(f"回测期间：{start_date} ~ {end_date}")
    logger.info(f"初始资金：{args.capital}U")
    
    # 运行回测
    report = run_backtest_v55_full(
        data=data,
        start_date=start_date,
        end_date=end_date,
        capital=Decimal(args.capital)
    )
    
    # 打印报告
    print("\n" + "=" * 80)
    print("v5.5 平衡优化版回测报告（全量多时间框架分析）")
    print("=" * 80)
    
    if 'summary' not in report:
        print(f"\n⚠️ {report.get('message', '未知错误')}")
        return
    
    s = report['summary']
    assess = report.get('performance_assessment', {})
    
    print(f"\n📊 基础统计")
    print(f"  总交易：{s['total_trades']} 笔")
    print(f"  盈利：{s['winning_trades']} 笔 | 亏损：{s['losing_trades']} 笔")
    print(f"  手续费：{s['total_fees']:.2f}U")
    
    print(f"\n💰 盈利能力")
    print(f"  初始：500U → 最终：{s['final_capital']:.2f}U")
    print(f"  总盈亏：{s['total_pnl']:.2f}U")
    print(f"  收益率：{s['total_return']:.1%}")
    
    print(f"\n📈 稳定性")
    print(f"  胜率：{s['win_rate']:.1%} ({assess.get('win_rate', 'N/A')})")
    print(f"  盈亏比：{s['profit_loss_ratio']:.2f} ({assess.get('profit_loss_ratio', 'N/A')})")
    
    print(f"\n🏆 综合评估：{assess.get('overall', 'N/A')}")
    
    if report.get('grade_statistics'):
        print(f"\n📊 按信号等级")
        print(f"  {'等级':<6} {'交易数':<8} {'胜率':<10} {'总盈亏':<12}")
        print(f"  {'-' * 40}")
        for grade in ['S', 'A', 'B']:
            if grade in report['grade_statistics']:
                st = report['grade_statistics'][grade]
                print(f"  {grade:<6} {st['trades']:<8} {st['win_rate']:<10.1%} {st['total_pnl']:<12.2f}U")
    
    if report.get('trades'):
        print(f"\n📝 交易样本 (前 10)")
        print(f"  {'#':<4} {'币种':<10} {'方向':<6} {'入场':<10} {'出场':<10} {'盈亏':<12} {'原因':<15}")
        print(f"  {'-' * 80}")
        for i, t in enumerate(report['trades'][:10], 1):
            print(f"  {i:<4} {t['symbol']:<10} {t['direction']:<6} "
                  f"{float(t['entry_price']):<10.2f} {float(t['exit_price']):<10.2f} "
                  f"{float(t['pnl']):<12.2f}U {t['exit_reason']:<15}")
    
    # 保存报告
    output_path = Path(args.output)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    
    logger.info(f"\n✅ 报告已保存到：{output_path}")


if __name__ == '__main__':
    main()
