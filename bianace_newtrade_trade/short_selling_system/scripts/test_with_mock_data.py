#!/usr/bin/env python3
"""
使用模拟数据测试回测系统
"""

import json
import random
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.short_selling_backtester import run_short_backtest
from backtesting.performance_analyzer import PerformanceAnalyzer
from backtesting.report_generator import ReportGenerator


def generate_mock_klines(base_price: float, count: int, trend: str = 'down') -> list:
    """生成模拟 K 线数据"""
    klines = []
    current_price = base_price
    start_date = datetime.now() - timedelta(hours=count)
    
    for i in range(count):
        timestamp = start_date + timedelta(hours=i)
        
        if trend == 'down':
            change = random.uniform(-0.03, 0.015)
        elif trend == 'up':
            change = random.uniform(-0.015, 0.03)
        else:
            change = random.uniform(-0.02, 0.02)
        
        open_price = current_price
        close_price = current_price * (1 + change)
        high_price = max(open_price, close_price) * (1 + random.uniform(0, 0.02))
        low_price = min(open_price, close_price) * (1 - random.uniform(0, 0.02))
        volume = random.uniform(10000, 100000)
        
        kline = {
            'timestamp': timestamp.isoformat(),
            'open': str(open_price),
            'high': str(high_price),
            'low': str(low_price),
            'close': str(close_price),
            'volume': str(volume)
        }
        klines.append(kline)
        
        current_price = close_price
    
    return klines


def generate_mock_data() -> dict:
    """生成模拟的回测数据"""
    symbols = [
        'PRLUSDT', 'NATGASUSDT', 'BAUSDT', 'BZUSDT',
        'CLUSDT', 'BASEUSDT', 'GOOGLUSDT', 'NVDAUSDT',
        'METAUSDT', 'XAUTUSDT', 'BSBUSDT', 'PAYPUSDT'
    ]
    
    base_prices = {
        'PRLUSDT': 0.5, 'NATGASUSDT': 3.5, 'BAUSDT': 15.0, 'BZUSDT': 8.0,
        'CLUSDT': 70.0, 'BASEUSDT': 1.2, 'GOOGLUSDT': 140.0, 'NVDAUSDT': 120.0,
        'METAUSDT': 350.0, 'XAUTUSDT': 2000.0, 'BSBUSDT': 250.0, 'PAYPUSDT': 0.8
    }
    
    data = {}
    for symbol in symbols:
        print(f"生成 {symbol} 模拟数据...")
        
        data[symbol] = {
            'symbol_info': {
                'symbol': symbol,
                'contractType': 'PERPETUAL',
                'listTime': int((datetime.now() - timedelta(days=30)).timestamp() * 1000),
                'baseAsset': symbol.replace('USDT', ''),
                'quoteAsset': 'USDT'
            },
            '1d': generate_mock_klines(base_prices[symbol], 90, trend='down'),
            '4h': generate_mock_klines(base_prices[symbol], 90 * 6, trend='down'),
            '1h': generate_mock_klines(base_prices[symbol], 90 * 24, trend='down'),
            'funding_rate': [
                {
                    'timestamp': (datetime.now() - timedelta(hours=i)).isoformat(),
                    'fundingRate': str(random.uniform(0.0001, 0.001))
                }
                for i in range(90 * 3)
            ],
            'open_interest': {
                'symbol': symbol,
                'openInterest': str(random.uniform(10000000, 50000000)),
                'timestamp': datetime.now().isoformat()
            }
        }
        
        print(f"  ✅ 1h: {len(data[symbol]['1h'])} 条")
    
    return data


def main():
    print("=" * 80)
    print("做空策略回测系统 - 模拟数据测试")
    print("=" * 80)
    
    print("\n生成模拟数据...")
    mock_data = generate_mock_data()
    
    data_path = Path('data/mock_backtest_data.json')
    data_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(data_path, 'w', encoding='utf-8') as f:
        json.dump(mock_data, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ 模拟数据已保存到：{data_path}")
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=90)
    
    print(f"\n回测期间：{start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
    print("初始资金：500 USDT")
    
    print("\n" + "=" * 80)
    print("开始运行回测...")
    print("=" * 80)
    
    report = run_short_backtest(
        data_path=str(data_path),
        start_date=start_date,
        end_date=end_date,
        capital=Decimal('500')
    )
    
    print("\n" + "=" * 80)
    print("回测完成！生成分析报告...")
    print("=" * 80)
    
    analyzer = PerformanceAnalyzer(report)
    analysis = analyzer.full_analysis()
    
    output_json = 'data/mock_backtest_report.json'
    output_md = 'data/mock_backtest_report.md'
    
    generator = ReportGenerator(report, analysis)
    result = generator.generate_all(output_json, output_md)
    
    print(f"\n✅ JSON 报告：{result['json_report']}")
    print(f"✅ Markdown 报告：{result['markdown_report']}")
    
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
    print("✅ 模拟测试完成！")
    print("=" * 80)


if __name__ == '__main__':
    main()
