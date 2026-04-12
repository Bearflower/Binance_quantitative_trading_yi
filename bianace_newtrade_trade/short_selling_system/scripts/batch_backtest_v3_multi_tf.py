#!/usr/bin/env python3
"""
批量回测脚本 v3 - 多时间框架回测
支持 5m/15m/30m/1h 四个时间框架同时回测
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List

# 导入回测组件
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.short_selling_backtester_v4 import ShortSellingBacktesterV4


def prepare_backtest_data(all_data: Dict, symbol: str) -> Dict:
    """准备单个币种的回测数据"""
    
    symbol_data = all_data['data'].get(symbol, {})
    
    if not symbol_data:
        return None
    
    # 构造回测器期望的格式
    result = {
        symbol: {
            'symbol_info': {
                'symbol': symbol,
                'baseAsset': all_data['data'][symbol].get('baseAsset', symbol.replace('USDT', '')),
                'quoteAsset': 'USDT',
                'listTime': all_data['data'][symbol].get('listTime', 0)
            },
            'funding_rate': [],
        }
    }
    
    # 添加各时间框架的 K 线到第一层
    for interval in ['1d', '4h', '1h', '30m', '15m', '5m']:
        if interval in symbol_data:
            result[symbol][interval] = symbol_data[interval]['data']
        else:
            result[symbol][interval] = []
    
    return result


def multi_timeframe_backtest(data_file: str, 
                             output_dir: str,
                             capital: float = 500,
                             days: int = 90,
                             timeframes: List[str] = None,
                             config: Dict = None):
    """多时间框架批量回测"""
    
    if timeframes is None:
        timeframes = ['1h', '30m', '15m', '5m']
    
    print("=" * 80)
    print("批量回测 v3 - 多时间框架对比（新币做空策略 V2.0）")
    print("=" * 80)
    
    print(f"\n加载数据：{data_file}")
    with open(data_file, 'r', encoding='utf-8') as f:
        all_data = json.load(f)
    
    symbols = all_data['metadata']['symbols']
    print(f"币种数量：{len(symbols)}")
    print(f"回测参数：")
    print(f"  - 资金：${capital}")
    print(f"  - 天数：{days}")
    print(f"  - 时间框架：{', '.join(timeframes)}")
    print(f"  - 策略：一币一单 + 衰竭形态")
    print("=" * 80)
    
    # 准备回测
    output = Path(output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    
    # 存储所有时间框架的结果
    all_results = {}
    
    # 对每个时间框架进行回测
    for timeframe in timeframes:
        print(f"\n{'=' * 80}")
        print(f"开始回测时间框架：{timeframe}")
        print(f"{'=' * 80}")
        
        results = []
        total_trades = 0
        total_pnl = Decimal('0')
        successful_trades = []
        losing_trades = []
        
        # 逐个币种回测
        for idx, symbol in enumerate(symbols, 1):
            print(f"\n[{idx}/{len(symbols)}] 回测 {symbol}...")
            
            try:
                # 准备数据
                backtest_data = prepare_backtest_data(all_data, symbol)
                
                if not backtest_data:
                    print(f"  ⚠️ 数据不足，跳过")
                    continue
                
                # 创建回测器
                backtester = ShortSellingBacktesterV4(config=config)
                
                # 计算回测日期范围
                end_date = datetime.now()
                start_date = end_date - timedelta(days=days)
                
                # 运行回测
                result = backtester.run_backtest(
                    data=backtest_data,
                    start_date=start_date,
                    end_date=end_date,
                    timeframe=timeframe
                )
                
                if result and result.get('summary'):
                    summary = result['summary']
                    trades = result.get('trades', [])
                    
                    # 记录结果
                    if trades:
                        for trade in trades:
                            coin_result = {
                                'symbol': symbol,
                                'total_trades': 1,
                                'winning_trades': 1 if trade['pnl'] > 0 else 0,
                                'losing_trades': 0 if trade['pnl'] > 0 else 1,
                                'win_rate': 1.0 if trade['pnl'] > 0 else 0.0,
                                'total_pnl': float(trade['pnl']),
                                'exit_reason': trade['exit_reason'],
                                'trade': trade
                            }
                            
                            results.append(coin_result)
                            total_trades += 1
                            total_pnl += Decimal(str(trade['pnl']))
                            
                            if trade['pnl'] > 0:
                                successful_trades.append(symbol)
                                print(f"  ✅ 盈利：${trade['pnl']:.2f} ({trade['exit_reason']})")
                            else:
                                losing_trades.append(symbol)
                                print(f"  ❌ 亏损：${trade['pnl']:.2f} ({trade['exit_reason']})")
                    else:
                        print(f"  ⚠️ 无交易")
                else:
                    print(f"  ⚠️ 回测失败")
            
            except Exception as e:
                print(f"  ❌ 错误：{e}")
                import traceback
                traceback.print_exc()
        
        # 汇总统计
        if results:
            # 总体统计
            total_coins = len(results)
            profitable_coins = len(successful_trades)
            loss_coins = len(losing_trades)
            
            print(f"\n{'=' * 80}")
            print(f"{timeframe} 时间框架汇总")
            print(f"{'=' * 80}")
            print(f"\n总体表现:")
            print(f"  交易币种：{total_coins} 个")
            print(f"  盈利交易：{profitable_coins} 个 ({profitable_coins/total_coins*100:.1f}%)")
            print(f"  亏损交易：{loss_coins} 个 ({loss_coins/total_coins*100:.1f}%)")
            print(f"  总交易次数：{total_trades} 笔")
            print(f"  总盈亏：${float(total_pnl):.2f}")
            
            # 平均表现
            avg_pnl = float(total_pnl) / total_coins
            win_rate = profitable_coins / total_coins * 100
            
            print(f"\n平均表现:")
            print(f"  平均盈亏：${avg_pnl:.2f}")
            print(f"  胜率：{win_rate:.1f}%")
            
            # 最佳和最差
            best_trade = max(results, key=lambda x: x['total_pnl'])
            worst_trade = min(results, key=lambda x: x['total_pnl'])
            
            print(f"\n最佳交易：{best_trade['symbol']}")
            print(f"  盈亏：${best_trade['total_pnl']:.2f}")
            print(f"  出场原因：{best_trade['exit_reason']}")
            
            print(f"\n最差交易：{worst_trade['symbol']}")
            print(f"  盈亏：${worst_trade['total_pnl']:.2f}")
            print(f"  出场原因：{worst_trade['exit_reason']}")
            
            # 保存该时间框架的结果
            all_results[timeframe] = {
                'metadata': {
                    'backtest_time': datetime.now().isoformat(),
                    'total_symbols': len(symbols),
                    'traded_symbols': total_coins,
                    'timeframe': timeframe,
                    'capital': capital,
                    'days': days,
                    'strategy': '一币一单 V2.0'
                },
                'summary': {
                    'total_coins': total_coins,
                    'profitable_coins': profitable_coins,
                    'loss_coins': loss_coins,
                    'total_trades': total_trades,
                    'total_pnl': float(total_pnl),
                    'avg_pnl': avg_pnl,
                    'win_rate': win_rate,
                    'best_symbol': best_trade['symbol'],
                    'best_pnl': best_trade['total_pnl'],
                    'worst_symbol': worst_trade['symbol'],
                    'worst_pnl': worst_trade['total_pnl']
                },
                'results': results,
                'successful_trades': successful_trades,
                'losing_trades': losing_trades
            }
    
    # 保存所有时间框架的结果
    if all_results:
        # 保存 JSON
        json_output = output_dir + '_summary.json'
        with open(json_output, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        print(f"\n{'=' * 80}")
        print(f"✅ 详细结果已保存到：{json_output}")
        
        # 生成 Markdown 对比报告
        generate_multi_timeframe_report(all_results, output_dir + '.md', timeframes)
        print(f"✅ 对比报告已保存到：{output_dir}.md")
    
    print(f"\n{'=' * 80}")
    print("多时间框架回测完成！")
    print(f"{'=' * 80}")


def generate_multi_timeframe_report(all_results: Dict, output_path: str, timeframes: List[str]):
    """生成多时间框架对比报告"""
    
    md = f"""# 新币做空策略 V2.0 - 多时间框架回测对比报告

## 基本信息

- **回测时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **币种总数**: {all_results[timeframes[0]]['metadata']['total_symbols']} 个
- **初始资金**: ${all_results[timeframes[0]]['metadata']['capital']}
- **回测天数**: {all_results[timeframes[0]]['metadata']['days']} 天
- **策略**: 一币一单 + 衰竭形态

## 各时间框架对比

| 时间框架 | 交易币种 | 盈利交易 | 亏损交易 | 胜率 | 总盈亏 | 平均盈亏 |
|---------|---------|---------|---------|------|--------|---------|
"""
    
    # 添加各时间框架数据
    for tf in timeframes:
        if tf in all_results:
            summary = all_results[tf]['summary']
            md += f"| {tf} | {summary['total_coins']} | {summary['profitable_coins']} | {summary['loss_coins']} | {summary['win_rate']:.1f}% | ${summary['total_pnl']:.2f} | ${summary['avg_pnl']:.2f} |\n"
    
    md += f"\n## 各时间框架详细分析\n"
    
    # 对每个时间框架详细分析
    for tf in timeframes:
        if tf not in all_results:
            continue
        
        result = all_results[tf]
        metadata = result['metadata']
        summary = result['summary']
        
        md += f"\n### {tf} 时间框架\n\n"
        md += f"- **交易币种**: {summary['total_coins']} 个\n"
        md += f"- **盈利交易**: {summary['profitable_coins']} 个 ({summary['win_rate']:.1f}%)\n"
        md += f"- **总盈亏**: ${summary['total_pnl']:.2f}\n"
        md += f"- **平均盈亏**: ${summary['avg_pnl']:.2f}\n"
        md += f"- **最佳交易**: {summary['best_symbol']} (${summary['best_pnl']:.2f})\n"
        md += f"- **最差交易**: {summary['worst_symbol']} (${summary['worst_pnl']:.2f})\n"
        
        # 前 10 个盈利交易
        md += f"\n**前 10 个盈利交易**:\n\n"
        sorted_results = sorted(result['results'], key=lambda x: x['total_pnl'], reverse=True)[:10]
        for i, r in enumerate(sorted_results, 1):
            md += f"{i}. **{r['symbol']}**: ${r['total_pnl']:.2f} ({r['exit_reason']})\n"
    
    md += f"\n## 结论与建议\n\n"
    
    # 找出最佳时间框架
    best_tf = max(timeframes, key=lambda tf: all_results.get(tf, {}).get('summary', {}).get('total_pnl', -999999))
    worst_tf = min(timeframes, key=lambda tf: all_results.get(tf, {}).get('summary', {}).get('total_pnl', -999999))
    
    md += f"1. **最佳时间框架**: {best_tf}（总盈亏 ${all_results[best_tf]['summary']['total_pnl']:.2f}）\n"
    md += f"2. **最差时间框架**: {worst_tf}（总盈亏 ${all_results[worst_tf]['summary']['total_pnl']:.2f}）\n"
    md += f"3. **建议**: 优先使用 {best_tf} 时间框架进行交易，该框架下胜率最高且盈亏比最优。\n"
    
    md += f"\n---\n\n*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n"
    
    # 保存
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='批量回测 v3 - 多时间框架')
    parser.add_argument('--data', type=str, default='data/2025_new_coins_data.json',
                       help='数据文件路径')
    parser.add_argument('--output', type=str, default='data/batch_backtest_v3_multi_tf',
                       help='输出文件路径前缀')
    parser.add_argument('--capital', type=float, default=500,
                       help='初始资金')
    parser.add_argument('--days', type=int, default=90,
                       help='回测天数')
    parser.add_argument('--timeframes', type=str, nargs='+', default=['1h', '30m', '15m', '5m'],
                       help='时间框架列表')
    
    args = parser.parse_args()
    
    # 回测配置
    config = {
        'initial_capital': Decimal(str(args.capital)),
        'backtest_days': args.days,
        'leverage': 3,
        'risk_per_trade': Decimal('0.02'),  # 2%
        'max_listing_hours': Decimal('48'),  # 48 小时窗口
        'stop_loss_atr_multiplier': Decimal('2.0'),
        'min_stop_loss_pct': Decimal('0.04'),
        'take_profit_1_atr': Decimal('1.5'),
        'take_profit_2_atr': Decimal('3.0'),
        'time_stop_hours': 48,
    }
    
    # 运行多时间框架回测
    multi_timeframe_backtest(
        data_file=args.data,
        output_dir=args.output,
        capital=args.capital,
        days=args.days,
        timeframes=args.timeframes,
        config=config
    )


if __name__ == '__main__':
    main()
