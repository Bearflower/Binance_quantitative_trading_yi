#!/usr/bin/env python3
"""
批量回测脚本 v2 - "一币一单"策略
使用 backtester v3，每个币种只交易一次
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List

# 导入回测组件
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.short_selling_backtester_v3 import ShortSellingBacktesterV3


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
                'quoteAsset': 'USDT'
            },
            'funding_rate': [],
        }
    }
    
    # 添加各时间框架的 K 线到第一层
    for interval in ['1d', '4h', '1h', '15m', '5m']:
        if interval in symbol_data:
            result[symbol][interval] = symbol_data[interval]['data']
        else:
            result[symbol][interval] = []
    
    return result


def batch_backtest_v2(data_file: str, 
                      output_dir: str,
                      capital: float = 500,
                      days: int = 90,
                      timeframe: str = '1h',
                      config: Dict = None):
    """批量回测所有币种 - 一币一单策略"""
    
    print("=" * 80)
    print("批量回测 v2 - 一币一单狙击策略")
    print("=" * 80)
    
    print(f"\n加载数据：{data_file}")
    with open(data_file, 'r', encoding='utf-8') as f:
        all_data = json.load(f)
    
    symbols = all_data['metadata']['symbols']
    print(f"币种数量：{len(symbols)}")
    print(f"回测参数：")
    print(f"  - 资金：${capital}")
    print(f"  - 天数：{days}")
    print(f"  - 时间框架：{timeframe}")
    print(f"  - 策略：每个币种只交易一次")
    print("=" * 80)
    
    # 准备回测
    output = Path(output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    
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
            backtester = ShortSellingBacktesterV3(config=config)
            
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
    print("\n" + "=" * 80)
    print("批量回测汇总")
    print("=" * 80)
    
    if results:
        # 总体统计
        total_coins = len(results)
        profitable_coins = len(successful_trades)
        loss_coins = len(losing_trades)
        
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
        
        # 保存详细结果
        summary = {
            'metadata': {
                'backtest_time': datetime.now().isoformat(),
                'total_symbols': len(symbols),
                'traded_symbols': total_coins,
                'timeframe': timeframe,
                'capital': capital,
                'days': days,
                'strategy': '一币一单'
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
        
        # 保存 JSON
        json_output = output_dir + '_summary.json'
        with open(json_output, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 详细结果已保存到：{json_output}")
        
        # 生成 Markdown 报告
        generate_markdown_report_v2(summary, output_dir + '.md')
        print(f"✅ 报告已保存到：{output_dir}.md")
    
    print("\n" + "=" * 80)


def generate_markdown_report_v2(summary: Dict, output_path: str):
    """生成 Markdown 格式的回测报告 v2"""
    
    metadata = summary['metadata']
    summ = summary['summary']
    results = summary['results']
    
    md = f"""# 一币一单策略批量回测报告

## 基本信息

- **回测时间**: {metadata['backtest_time']}
- **币种总数**: {metadata['total_symbols']} 个
- **交易币种**: {summ['total_coins']} 个
- **时间框架**: {metadata['timeframe']}
- **初始资金**: ${metadata['capital']}
- **回测天数**: {metadata['days']} 天
- **策略**: {metadata['strategy']}

## 总体表现

| 指标 | 数值 |
|------|------|
| 盈利交易 | {summ['profitable_coins']} 个 ({summ['profitable_coins']/summ['total_coins']*100:.1f}%) |
| 亏损交易 | {summ['loss_coins']} 个 ({summ['loss_coins']/summ['total_coins']*100:.1f}%) |
| 总交易次数 | {summ['total_trades']} 笔 |
| 总盈亏 | ${summ['total_pnl']:.2f} |
| 平均盈亏 | ${summ['avg_pnl']:.2f} |
| 胜率 | {summ['win_rate']:.1f}% |

## 最佳交易

**最佳币种**: {summ['best_symbol']}
- 盈亏：${summ['best_pnl']:.2f}

**最差币种**: {summ['worst_symbol']}
- 盈亏：${summ['worst_pnl']:.2f}

## 盈利交易列表

共 {len(summary['successful_trades'])} 个：

"""
    
    # 添加盈利交易
    for i, symbol in enumerate(summary['successful_trades'][:30], 1):
        result = next(r for r in results if r['symbol'] == symbol)
        md += f"{i}. **{symbol}**: ${result['total_pnl']:.2f} ({result['exit_reason']})\n"
    
    if len(summary['successful_trades']) > 30:
        md += f"\n... 还有 {len(summary['successful_trades']) - 30} 个\n"
    
    md += f"\n## 亏损交易列表\n\n共 {len(summary['losing_trades'])} 个：\n\n"
    
    # 添加亏损交易
    for i, symbol in enumerate(summary['losing_trades'][:30], 1):
        result = next(r for r in results if r['symbol'] == symbol)
        md += f"{i}. **{symbol}**: ${result['total_pnl']:.2f} ({result['exit_reason']})\n"
    
    if len(summary['losing_trades']) > 30:
        md += f"\n... 还有 {len(summary['losing_trades']) - 30} 个\n"
    
    md += f"\n---\n\n*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n"
    
    # 保存
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='批量回测 v2 - 一币一单策略')
    parser.add_argument('--data', type=str, default='data/2025_new_coins_data.json',
                       help='数据文件路径')
    parser.add_argument('--output', type=str, default='data/batch_backtest_v2_onecoin',
                       help='输出文件路径前缀')
    parser.add_argument('--capital', type=float, default=500,
                       help='初始资金')
    parser.add_argument('--days', type=int, default=90,
                       help='回测天数')
    parser.add_argument('--timeframe', type=str, default='1h',
                       choices=['5m', '15m', '1h', '4h', '1d'],
                       help='时间框架')
    
    args = parser.parse_args()
    
    # 回测配置
    config = {
        'use_coin_filter': True,
        'coin_filter_config': {
            'min_funding_rate_annual': Decimal('1.0'),  # 年化费率≥100%
            'max_oi_to_market_cap': Decimal('0.5'),    # OI/市值比≤0.5
            'min_unlock_percentage': Decimal('0.10'),   # 解锁比例≥10%
            'max_listing_hours': Decimal('168'),        # 上线≤7 天
        },
        'signal_config': {
            'stop_loss_atr_multiplier': Decimal('2.5'),  # 2.5 倍 ATR 止损
            'take_profit_levels': [
                {'percentage': Decimal('0.15'), 'close_ratio': Decimal('0.30')},  # 15% 止盈 30% 仓
                {'percentage': Decimal('0.30'), 'close_ratio': Decimal('0.40')},  # 30% 止盈 40% 仓
            ],
            'trailing_stop': {
                'enabled': True,
                'activation_profit': Decimal('0.10'),  # 浮盈 10% 启动
                'trailing_percent': Decimal('0.05'),   # 5% 移动止损
            },
            'time_stop_hours': 72,  # 72 小时时间止损
        }
    }
    
    # 运行批量回测
    batch_backtest_v2(
        data_file=args.data,
        output_dir=args.output,
        capital=args.capital,
        days=args.days,
        timeframe=args.timeframe,
        config=config
    )


if __name__ == '__main__':
    main()
