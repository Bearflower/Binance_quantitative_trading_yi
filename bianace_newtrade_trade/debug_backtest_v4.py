#!/usr/bin/env python3
"""
调试V4.0回测为什么没有交易
"""

import json
import sys
from pathlib import Path
from decimal import Decimal
import random

sys.path.insert(0, str(Path(__file__).parent))

from core.scoring_engine_v4 import scoring_engine_v4
from core.pattern_recognition_v4 import pattern_recognition_v4


def debug_backtest(data_file: str):
    """调试回测"""

    print(f"\n{'='*80}")
    print(f"调试V4.0回测")
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
        'patterns_detected': 0,
        'technical_requirements_met': 0,
        'scores_calculated': 0,
        'signals_found': 0,
        'veto_by_oi': 0,
        'veto_by_time': 0,
        'score_too_low': 0
    }

    for symbol, symbol_data in list(symbols_data.items())[:20]:
        klines_1h_dict = symbol_data.get('1h', {})

        if isinstance(klines_1h_dict, dict):
            klines_1h = klines_1h_dict.get('data', [])
        else:
            klines_1h = klines_1h_dict

        if not klines_1h or len(klines_1h) < 10:
            continue

        stats['symbols_with_klines'] += 1

        print(f"\n处理 {symbol} ({len(klines_1h)} 根K线)...")

        for i in range(10, min(len(klines_1h), 30)):
            current_klines = klines_1h[:i+1]
            current_kline = klines_1h[i]

            pattern_result = pattern_recognition_v4.analyze_patterns(current_klines)

            if pattern_result['data_insufficient']:
                continue

            stats['symbols_analyzed'] += 1

            three_tops_score = pattern_result['three_tops']['score']
            technical_score = pattern_result['total_score']

            if three_tops_score > 0 or technical_score > 0:
                stats['patterns_detected'] += 1

            technical_ok, reason = scoring_engine_v4.check_technical_requirements(
                three_tops_score,
                technical_score
            )

            if technical_ok:
                stats['technical_requirements_met'] += 1

                oi_ratio = Decimal(str(random.uniform(0.1, 0.9)))
                contract_score, contract_reason = scoring_engine_v4.calculate_contract_score(float(oi_ratio))

                funding_rate = Decimal(str(random.uniform(-50, 200)))
                sentiment_score, sentiment_reason = scoring_engine_v4.calculate_sentiment_score(float(funding_rate))

                total_score = scoring_engine_v4.calculate_total_score(
                    contract_score,
                    technical_score,
                    sentiment_score
                )

                stats['scores_calculated'] += 1

                print(f"  K线 {i}: 技术得分={technical_score:.1f}, 合约得分={contract_score:.1f}, 情绪得分={sentiment_score:.1f}, 总分={total_score:.2f}")
                print(f"    三次冲顶={three_tops_score:.1f}, OI/市值比={oi_ratio:.4f}, 资金费率={funding_rate:.1f}%")

                if total_score >= scoring_engine_v4.entry_threshold:
                    stats['signals_found'] += 1
                    print(f"    ✅ 发现信号！")
                else:
                    stats['score_too_low'] += 1
                    print(f"    ❌ 总分不足 ({total_score:.2f} < {scoring_engine_v4.entry_threshold})")

                break
            else:
                print(f"  K线 {i}: 技术要求不满足 - {reason}")

    print(f"\n{'='*80}")
    print(f"调试完成")
    print(f"{'='*80}")
    print(f"\n📊 统计：")
    print(f"  总币种数: {stats['total_symbols']}")
    print(f"  有K线数据的币种: {stats['symbols_with_klines']}")
    print(f"  分析的K线数量: {stats['symbols_analyzed']}")
    print(f"  检测到形态的K线: {stats['patterns_detected']}")
    print(f"  满足技术要求的K线: {stats['technical_requirements_met']}")
    print(f"  计算得分的K线: {stats['scores_calculated']}")
    print(f"  发现信号的K线: {stats['signals_found']}")
    print(f"  OI/市值比否决: {stats['veto_by_oi']}")
    print(f"  时间否决: {stats['veto_by_time']}")
    print(f"  总分不足: {stats['score_too_low']}")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='调试V4.0回测')
    parser.add_argument('--data', type=str, default='data/2025_new_coins_data.json')

    args = parser.parse_args()

    debug_backtest(args.data)
