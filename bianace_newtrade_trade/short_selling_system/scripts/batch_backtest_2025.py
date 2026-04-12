#!/usr/bin/env python3
"""
批量回测脚本 - 对 2025 年所有新币进行回测
支持不同时间框架和参数配置
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List

# 导入回测组件
# 将项目根目录加入路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 现在可以导入 backtesting 模块
from backtesting.short_selling_backtester_v2 import ShortSellingBacktesterV2
from backtesting.signal_generator_v2 import SignalGeneratorV2
from backtesting.coin_filter import CoinFilter


def prepare_backtest_data(all_data: Dict, symbol: str) -> Dict:
    """准备单个币种的回测数据"""
    
    symbol_data = all_data['data'].get(symbol, {})
    
    if not symbol_data:
        return None
    
    # 提取各时间框架的 K 线，直接放在第一层
    result = {
        symbol: {
            'symbol_info': {
                'symbol': symbol,
                'baseAsset': all_data['data'][symbol].get('baseAsset', symbol.replace('USDT', '')),
                'quoteAsset': 'USDT'
            },
            'funding_rate': [],  # 暂时不提供费率数据
        }
    }
    
    # 添加各时间框架的 K 线到第一层
    for interval in ['1d', '4h', '1h', '15m', '5m']:
        if interval in symbol_data:
            result[symbol][interval] = symbol_data[interval]['data']
        else:
            result[symbol][interval] = []
    
    return result


def batch_backtest(data_file: str, 
                   output_dir: str,
                   capital: float = 500,
                   days: int = 90,
                   timeframe: str = '5m',
                   config: Dict = None):
    """批量回测所有币种"""
    
    # 1. 加载数据
    print("=" * 80)
    print("批量回测 - 2025 年新币")
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
    print("=" * 80)
    
    # 2. 准备回测
    output = Path(output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    
    results = []
    total_trades = 0
    total_pnl = Decimal('0')
    successful_coins = []
    losing_coins = []
    
    # 3. 逐个币种回测
    for idx, symbol in enumerate(symbols, 1):
        print(f"\n[{idx}/{len(symbols)}] 回测 {symbol}...")
        
        try:
            # 准备数据
            backtest_data = prepare_backtest_data(all_data, symbol)
            
            if not backtest_data:
                print(f"  ⚠️ 数据不足，跳过")
                continue
            
            # 检查是否有该时间框架的数据
            symbol_data = backtest_data.get(symbol, {})
            if not symbol_data.get(timeframe):
                print(f"  ⚠️ 缺少 {timeframe} 数据，跳过")
                continue
            
            # 计算回测日期范围
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            
            # 创建回测器
            backtester = ShortSellingBacktesterV2(
                config={
                    'initial_capital': Decimal(str(capital)),
                    'backtest_days': days,
                    **(config or {})
                }
            )
            
            # 运行回测
            result = backtester.run_backtest(
                data=backtest_data,
                start_date=start_date,
                end_date=end_date,
                timeframe=timeframe
            )
            
            if result and result.get('summary'):
                summary = result['summary']
                
                # 记录结果
                coin_result = {
                    'symbol': symbol,
                    'total_trades': summary.get('total_trades', 0),
                    'winning_trades': summary.get('winning_trades', 0),
                    'losing_trades': summary.get('losing_trades', 0),
                    'win_rate': float(summary.get('win_rate', 0)),
                    'total_pnl': float(summary.get('total_pnl', 0)),
                    'total_profit': float(summary.get('total_profit', 0)),
                    'total_loss': float(summary.get('total_loss', 0)),
                    'final_capital': float(summary.get('final_capital', 0)),
                    'return_percentage': float(summary.get('total_return', 0)),
                    'max_drawdown': float(summary.get('max_drawdown', 0)),
                    'sharpe_ratio': float(summary.get('sharpe_ratio', 0)),
                    'trades': result.get('trades', [])
                }
                
                results.append(coin_result)
                total_trades += summary.get('total_trades', 0)
                total_pnl += Decimal(str(summary.get('total_pnl', 0)))
                
                # 分类
                if summary.get('total_pnl', 0) > 0:
                    successful_coins.append(symbol)
                    print(f"  ✅ 盈利：${summary.get('total_pnl', 0):.2f} ({summary.get('total_return', 0):.2f}%)")
                else:
                    losing_coins.append(symbol)
                    print(f"  ❌ 亏损：${summary.get('total_pnl', 0):.2f} ({summary.get('total_return', 0):.2f}%)")
            else:
                print(f"  ⚠️ 回测失败")
        
        except Exception as e:
            print(f"  ❌ 错误：{e}")
            import traceback
            traceback.print_exc()
    
    # 4. 汇总统计
    print("\n" + "=" * 80)
    print("批量回测汇总")
    print("=" * 80)
    
    if results:
        # 总体统计
        total_coins = len(results)
        profitable_coins = len([r for r in results if r['total_pnl'] > 0])
        loss_coins = len([r for r in results if r['total_pnl'] <= 0])
        
        print(f"\n总体表现:")
        print(f"  回测币种：{total_coins} 个")
        print(f"  盈利币种：{profitable_coins} 个 ({profitable_coins/total_coins*100:.1f}%)")
        print(f"  亏损币种：{loss_coins} 个 ({loss_coins/total_coins*100:.1f}%)")
        print(f"  总交易次数：{total_trades} 笔")
        print(f"  总盈亏：${float(total_pnl):.2f}")
        
        # 平均表现
        avg_return = sum(r['return_percentage'] for r in results) / len(results)
        avg_win_rate = sum(r['win_rate'] for r in results) / len(results)
        
        print(f"\n平均表现:")
        print(f"  平均收益率：{avg_return:.2f}%")
        print(f"  平均胜率：{avg_win_rate:.2f}%")
        
        # 最佳和最差
        best_coin = max(results, key=lambda x: x['total_pnl'])
        worst_coin = min(results, key=lambda x: x['total_pnl'])
        
        print(f"\n最佳表现：{best_coin['symbol']}")
        print(f"  盈亏：${best_coin['total_pnl']:.2f} ({best_coin['return_percentage']:.2f}%)")
        print(f"  交易次数：{best_coin['total_trades']}")
        
        print(f"\n最差表现：{worst_coin['symbol']}")
        print(f"  盈亏：${worst_coin['total_pnl']:.2f} ({worst_coin['return_percentage']:.2f}%)")
        print(f"  交易次数：{worst_coin['total_trades']}")
        
        # 保存详细结果
        summary = {
            'metadata': {
                'backtest_time': datetime.now().isoformat(),
                'total_symbols': total_coins,
                'timeframe': timeframe,
                'capital': capital,
                'days': days
            },
            'summary': {
                'total_coins': total_coins,
                'profitable_coins': profitable_coins,
                'loss_coins': loss_coins,
                'total_trades': total_trades,
                'total_pnl': float(total_pnl),
                'avg_return': avg_return,
                'avg_win_rate': avg_win_rate,
                'best_coin': best_coin['symbol'],
                'best_return': best_coin['return_percentage'],
                'worst_coin': worst_coin['symbol'],
                'worst_return': worst_coin['return_percentage']
            },
            'results': results,
            'successful_coins': successful_coins,
            'losing_coins': losing_coins
        }
        
        # 保存 JSON
        json_output = output_dir + '_summary.json'
        with open(json_output, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 详细结果已保存到：{json_output}")
        
        # 生成 Markdown 报告
        generate_markdown_report(summary, output_dir + '.md')
        print(f"✅ 报告已保存到：{output_dir}.md")
    
    print("\n" + "=" * 80)


def generate_markdown_report(summary: Dict, output_path: str):
    """生成 Markdown 格式的回测报告"""
    
    metadata = summary['metadata']
    summ = summary['summary']
    results = summary['results']
    
    md = f"""# 2025 年新币批量回测报告

## 基本信息

- **回测时间**: {metadata['backtest_time']}
- **币种数量**: {summ['total_coins']} 个
- **时间框架**: {metadata['timeframe']}
- **初始资金**: ${metadata['capital']}
- **回测天数**: {metadata['days']} 天

## 总体表现

| 指标 | 数值 |
|------|------|
| 盈利币种 | {summ['profitable_coins']} 个 ({summ['profitable_coins']/summ['total_coins']*100:.1f}%) |
| 亏损币种 | {summ['loss_coins']} 个 ({summ['loss_coins']/summ['total_coins']*100:.1f}%) |
| 总交易次数 | {summ['total_trades']} 笔 |
| 总盈亏 | ${summ['total_pnl']:.2f} |
| 平均收益率 | {summ['avg_return']:.2f}% |
| 平均胜率 | {summ['avg_win_rate']:.2f}% |

## 最佳表现

**最佳币种**: {summ['best_coin']}
- 盈亏：${results[[r['symbol'] for r in results].index(summ['best_coin'])]['total_pnl']:.2f}
- 收益率：{summ['best_return']:.2f}%

**最差币种**: {summ['worst_coin']}
- 盈亏：${results[[r['symbol'] for r in results].index(summ['worst_coin'])]['total_pnl']:.2f}
- 收益率：{summ['worst_return']:.2f}%

## 盈利币种列表

共 {len(summary['successful_coins'])} 个：

{', '.join(summary['successful_coins'][:20])}

{'... 更多' if len(summary['successful_coins']) > 20 else ''}

## 亏损币种列表

共 {len(summary['losing_coins'])} 个：

{', '.join(summary['losing_coins'][:20])}

{'... 更多' if len(summary['losing_coins']) > 20 else ''}

## 详细数据

前 20 个币种详细表现：

| 币种 | 交易次数 | 胜率 | 盈亏 | 收益率 |
|------|---------|------|------|--------|
"""
    
    # 添加前 20 个币种
    for r in results[:20]:
        md += f"| {r['symbol']} | {r['total_trades']} | {r['win_rate']:.1f}% | ${r['total_pnl']:.2f} | {r['return_percentage']:.2f}% |\n"
    
    md += f"\n---\n\n*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n"
    
    # 保存
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='批量回测 2025 年新币')
    parser.add_argument('--data', type=str, default='data/2025_new_coins_data.json',
                       help='数据文件路径')
    parser.add_argument('--output', type=str, default='data/batch_backtest_2025',
                       help='输出文件路径前缀')
    parser.add_argument('--capital', type=float, default=500,
                       help='初始资金')
    parser.add_argument('--days', type=int, default=90,
                       help='回测天数')
    parser.add_argument('--timeframe', type=str, default='5m',
                       choices=['5m', '15m', '1h', '4h', '1d'],
                       help='时间框架')
    
    args = parser.parse_args()
    
    # 回测配置
    config = {
        'use_coin_filter': False,  # 暂时禁用币种筛选
        'coin_filter_config': {
            'min_funding_rate_annual': Decimal('1.0'),  # 年化费率≥100%
            'max_oi_to_market_cap': Decimal('0.5'),    # OI/市值比≤0.5
            'min_unlock_percentage': Decimal('0.10'),   # 解锁比例≥10%
            'max_listing_hours': Decimal('168'),        # 上线≤7 天
            'min_win_rate_history': Decimal('0.30'),    # 历史胜率≥30%
        },
        'signal_config': {
            'stop_loss_atr_multiplier': Decimal('2.0'),  # 2 倍 ATR 止损
            'take_profit_levels': [
                {'percentage': Decimal('0.10'), 'close_ratio': Decimal('0.30')},  # 10% 止盈 30% 仓
                {'percentage': Decimal('0.20'), 'close_ratio': Decimal('0.40')},  # 20% 止盈 40% 仓
            ],
            'trailing_stop': {
                'enabled': True,
                'activation_profit': Decimal('0.05'),  # 浮盈 5% 启动
                'trailing_percent': Decimal('0.03'),   # 3% 移动止损
            },
            'time_stop_hours': 72,  # 72 小时时间止损
        }
    }
    
    # 运行批量回测
    batch_backtest(
        data_file=args.data,
        output_dir=args.output,
        capital=args.capital,
        days=args.days,
        timeframe=args.timeframe,
        config=config
    )


if __name__ == '__main__':
    main()
