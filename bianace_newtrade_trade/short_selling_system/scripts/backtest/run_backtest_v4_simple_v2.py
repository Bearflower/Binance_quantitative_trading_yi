#!/usr/bin/env python3
"""
V4.0 简化版回测脚本（修正版）
专注于形态识别和评分逻辑验证
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).parent))

from core.scoring_engine_v4 import scoring_engine_v4
from core.pattern_recognition_v4 import pattern_recognition_v4


def run_v4_backtest(data_file: str, output_file: str):
    """运行V4.0回测"""

    print(f"\n{'='*80}")
    print(f"V4.0 简化版回测")
    print(f"{'='*80}")

    print(f"\n加载数据: {data_file}")
    with open(data_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    symbols_data = data.get('data', {})
    print(f"币种数量: {len(symbols_data)}")

    results = {
        'version': 'v4.0',
        'timestamp': datetime.now().isoformat(),
        'total_symbols': len(symbols_data),
        'symbols_with_signals': 0,
        'signals': []
    }

    for symbol, symbol_data in symbols_data.items():
        klines_1h_dict = symbol_data.get('1h', {})

        if isinstance(klines_1h_dict, dict):
            klines_1h = klines_1h_dict.get('data', [])
        else:
            klines_1h = klines_1h_dict

        if not klines_1h or len(klines_1h) < 10:
            continue

        print(f"\n处理 {symbol} ({len(klines_1h)} 根K线)...")

        for i in range(10, min(len(klines_1h), 50)):
            current_klines = klines_1h[:i+1]
            current_kline = klines_1h[i]

            pattern_result = pattern_recognition_v4.analyze_patterns(current_klines)

            if pattern_result['data_insufficient']:
                continue

            three_tops_score = pattern_result['three_tops']['score']
            technical_score = pattern_result['total_score']

            technical_ok, reason = scoring_engine_v4.check_technical_requirements(
                three_tops_score,
                technical_score
            )

            if not technical_ok:
                continue

            contract_score = 7.0
            sentiment_score = 5.0

            total_score = scoring_engine_v4.calculate_total_score(
                contract_score,
                technical_score,
                sentiment_score
            )

            if total_score >= scoring_engine_v4.entry_threshold:
                print(f"  ✅ 发现信号 @ {current_kline['close']}, 评分={total_score:.2f}")
                print(f"     三次冲顶: {pattern_result['three_tops']['score']:.1f}")
                print(f"     长上影线: {pattern_result['long_upper_shadow']['score']:.1f}")
                print(f"     放量滞涨: {pattern_result['volume_divergence']['score']:.1f}")

                signal = {
                    'symbol': symbol,
                    'timestamp': current_kline.get('timestamp', ''),
                    'price': float(current_kline['close']),
                    'score': float(total_score),
                    'pattern': pattern_result
                }

                results['signals'].append(signal)
                break

    results['symbols_with_signals'] = len(results['signals'])

    print(f"\n{'='*80}")
    print(f"回测完成")
    print(f"{'='*80}")
    print(f"\n📊 结果统计：")
    print(f"  总币种数: {results['total_symbols']}")
    print(f"  有信号币种: {results['symbols_with_signals']}")
    print(f"  信号比例: {results['symbols_with_signals']/results['total_symbols']:.1%}")

    print(f"\n保存报告: {output_file}")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)

    print(f"✅ 完成")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='V4.0 简化版回测')
    parser.add_argument('--data', type=str, default='data/2025_new_coins_data.json')
    parser.add_argument('--output', type=str, default='data/backtest_v4_simple.json')

    args = parser.parse_args()

    run_v4_backtest(args.data, args.output)
