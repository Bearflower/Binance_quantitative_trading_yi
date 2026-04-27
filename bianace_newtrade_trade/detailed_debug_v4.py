#!/usr/bin/env python3
"""
详细调试V4.0回测
检查每个币种的K线数据和评分
"""

import json
import sys
from pathlib import Path
from decimal import Decimal
import random

sys.path.insert(0, str(Path(__file__).parent))

from core.scoring_engine_v4 import scoring_engine_v4
from core.pattern_recognition_v4 import pattern_recognition_v4


def detailed_debug(data_file: str):
    """详细调试"""

    print(f"\n{'='*80}")
    print(f"详细调试V4.0回测")
    print(f"{'='*80}")

    print(f"\n加载数据: {data_file}")
    with open(data_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    symbols_data = data.get('data', {})
    print(f"币种数量: {len(symbols_data)}")

    for symbol, symbol_data in list(symbols_data.items())[:5]:
        print(f"\n{'='*80}")
        print(f"处理 {symbol}")
        print(f"{'='*80}")

        klines_1h_dict = symbol_data.get('1h', {})

        print(f"1h数据类型: {type(klines_1h_dict)}")

        if isinstance(klines_1h_dict, dict):
            print(f"1h数据键: {list(klines_1h_dict.keys())}")
            klines_1h = klines_1h_dict.get('data', [])
            print(f"K线数据类型: {type(klines_1h)}")
            print(f"K线数据长度: {len(klines_1h) if isinstance(klines_1h, list) else 'N/A'}")
        else:
            klines_1h = klines_1h_dict
            print(f"K线数据长度: {len(klines_1h) if isinstance(klines_1h, list) else 'N/A'}")

        if not klines_1h or len(klines_1h) < 10:
            print(f"❌ K线数据不足")
            continue

        print(f"\n前3根K线:")
        for i, kline in enumerate(klines_1h[:3]):
            print(f"  K线 {i}: {kline}")

        print(f"\n开始评分...")
        for i in range(10, min(len(klines_1h), 15)):
            current_klines = klines_1h[:i+1]
            current_kline = klines_1h[i]

            print(f"\nK线 {i}:")
            print(f"  时间: {current_kline.get('timestamp', 'N/A')}")
            print(f"  收盘价: {current_kline.get('close', 'N/A')}")

            pattern_result = pattern_recognition_v4.analyze_patterns(current_klines)

            if pattern_result['data_insufficient']:
                print(f"  ❌ 数据不足")
                continue

            three_tops_score = pattern_result['three_tops']['score']
            long_upper_shadow_score = pattern_result['long_upper_shadow']['score']
            volume_divergence_score = pattern_result['volume_divergence']['score']
            technical_score = pattern_result['total_score']

            print(f"  三次冲顶: {three_tops_score:.1f}")
            print(f"  长上影线: {long_upper_shadow_score:.1f}")
            print(f"  放量滞涨: {volume_divergence_score:.1f}")
            print(f"  技术总分: {technical_score:.1f}")

            technical_ok, reason = scoring_engine_v4.check_technical_requirements(
                three_tops_score,
                technical_score
            )

            if not technical_ok:
                print(f"  ❌ 技术要求不满足: {reason}")
                continue

            print(f"  ✅ 技术要求满足")

            oi_ratio = Decimal(str(random.uniform(0.1, 0.9)))
            contract_score, contract_reason = scoring_engine_v4.calculate_contract_score(float(oi_ratio))

            funding_rate = Decimal(str(random.uniform(-50, 200)))
            sentiment_score, sentiment_reason = scoring_engine_v4.calculate_sentiment_score(float(funding_rate))

            total_score = scoring_engine_v4.calculate_total_score(
                contract_score,
                technical_score,
                sentiment_score
            )

            print(f"  OI/市值比: {oi_ratio:.4f} -> 合约得分: {contract_score:.1f}")
            print(f"  资金费率: {funding_rate:.1f}% -> 情绪得分: {sentiment_score:.1f}")
            print(f"  综合得分: {total_score:.2f}")

            if total_score >= 6.0:
                print(f"  ✅✅✅ 发现信号！")
                break
            else:
                print(f"  ❌ 总分不足")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='详细调试V4.0回测')
    parser.add_argument('--data', type=str, default='data/2025_new_coins_data.json')

    args = parser.parse_args()

    detailed_debug(args.data)
