#!/usr/bin/env python3
"""
调试形态识别
"""

import json
import sys
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).parent))

from core.pattern_recognition_v4 import pattern_recognition_v4


def debug_pattern_recognition(data_file: str):
    """调试形态识别"""

    print(f"\n{'='*80}")
    print(f"调试形态识别")
    print(f"{'='*80}")

    print(f"\n加载数据: {data_file}")
    with open(data_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    symbols_data = data.get('data', {})
    print(f"币种数量: {len(symbols_data)}")

    stats = {
        'total_symbols': len(symbols_data),
        'symbols_with_klines': 0,
        'symbols_analyzed': 0,
        'three_tops_detected': 0,
        'long_upper_shadow_detected': 0,
        'volume_divergence_detected': 0,
        'technical_score_distribution': {}
    }

    for symbol, symbol_data in list(symbols_data.items())[:10]:
        klines_1h = symbol_data.get('1h', [])

        if not klines_1h or len(klines_1h) < 10:
            print(f"\n{symbol}: K线数据不足 ({len(klines_1h) if klines_1h else 0} < 10)")
            continue

        stats['symbols_with_klines'] += 1

        print(f"\n{symbol}: {len(klines_1h)} 根K线")

        for i in range(10, min(len(klines_1h), 20)):
            current_klines = klines_1h[:i+1]
            current_kline = klines_1h[i]

            pattern_result = pattern_recognition_v4.analyze_patterns(current_klines)

            if pattern_result['data_insufficient']:
                continue

            stats['symbols_analyzed'] += 1

            three_tops = pattern_result['three_tops']
            long_upper_shadow = pattern_result['long_upper_shadow']
            volume_divergence = pattern_result['volume_divergence']
            total_score = pattern_result['total_score']

            if three_tops['detected']:
                stats['three_tops_detected'] += 1
                print(f"  ✅ 三次冲顶 @ {current_kline['close']}, 得分={three_tops['score']:.1f}")

            if long_upper_shadow['detected']:
                stats['long_upper_shadow_detected'] += 1
                print(f"  ✅ 长上影线 @ {current_kline['close']}, 得分={long_upper_shadow['score']:.1f}")

            if volume_divergence['detected']:
                stats['volume_divergence_detected'] += 1
                print(f"  ✅ 放量滞涨 @ {current_kline['close']}, 得分={volume_divergence['score']:.1f}")

            score_range = f"{int(total_score)}-{int(total_score)+1}"
            stats['technical_score_distribution'][score_range] = stats['technical_score_distribution'].get(score_range, 0) + 1

            if total_score >= 6:
                print(f"  🎯 技术总分达标: {total_score:.1f}")

    print(f"\n{'='*80}")
    print(f"调试完成")
    print(f"{'='*80}")
    print(f"\n📊 统计：")
    print(f"  总币种数: {stats['total_symbols']}")
    print(f"  有K线数据的币种: {stats['symbols_with_klines']}")
    print(f"  分析的K线数量: {stats['symbols_analyzed']}")
    print(f"  检测到三次冲顶: {stats['three_tops_detected']}")
    print(f"  检测到长上影线: {stats['long_upper_shadow_detected']}")
    print(f"  检测到放量滞涨: {stats['volume_divergence_detected']}")
    print(f"\n  技术评分分布:")
    for score_range in sorted(stats['technical_score_distribution'].keys()):
        count = stats['technical_score_distribution'][score_range]
        print(f"    {score_range}分: {count}次")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='调试形态识别')
    parser.add_argument('--data', type=str, default='data/2025_new_coins_data.json')

    args = parser.parse_args()

    debug_pattern_recognition(args.data)
