#!/usr/bin/env python3
"""
检查所有入场机会
"""

import json
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'short_selling_system'))

# 直接导入，避免__init__.py的问题
import importlib.util
spec = importlib.util.spec_from_file_location("scoring_engine_v41", "short_selling_system/core/scoring_engine_v41.py")
scoring_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scoring_module)
ScoringEngineV41 = scoring_module.ScoringEngineV41

spec2 = importlib.util.spec_from_file_location("pattern_recognition_v4", "short_selling_system/core/pattern_recognition_v4.py")
pattern_module = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(pattern_module)
PatternRecognitionV4 = pattern_module.PatternRecognitionV4


def main():
    # 加载数据
    with open('short_selling_system/data/2025_new_coins_data.json', 'r') as f:
        kline_data = json.load(f)

    with open('short_selling_system/data/real_oi_funding_data.json', 'r') as f:
        real_data_json = json.load(f)

    real_data = {}
    for item in real_data_json['data']:
        symbol = item['symbol']
        real_data[symbol] = {
            'oi_usd': item.get('oi_usd'),
            'funding_rate': item.get('funding_rate', 0.00005)
        }

    symbols = kline_data.get('metadata', {}).get('symbols', [])
    data_dict = kline_data.get('data', {})

    scoring_engine = ScoringEngineV41()
    pattern_recognition = PatternRecognitionV4()

    # 统计所有符合条件的入场点
    all_entry_points = []
    total_klines_checked = 0
    
    for symbol in symbols:
        symbol_data = data_dict.get(symbol, {})
        klines_1h = symbol_data.get('1h', {}).get('data', [])
        
        if not klines_1h or len(klines_1h) < 20:
            continue
        
        oi_usd = real_data.get(symbol, {}).get('oi_usd', 0)
        funding_rate = real_data.get(symbol, {}).get('funding_rate', 0.00005)
        
        if not oi_usd:
            continue
        
        # 计算总交易量
        total_volume = sum(float(k.get('quote_volume', 0)) for k in klines_1h)
        
        # 检查每个K线是否符合入场条件
        entry_count = 0
        for i in range(20, len(klines_1h)):
            total_klines_checked += 1
            current_price = float(klines_1h[i]['close'])
            cumulative_volume = sum(float(klines_1h[j].get('quote_volume', 0)) for j in range(i+1))
            
            if cumulative_volume <= 0:
                cumulative_volume = total_volume
            
            pattern_result = pattern_recognition.analyze_patterns(klines_1h[:i+1])
            
            three_tops_score = pattern_result.get('three_tops', {}).get('score', 0)
            technical_score = pattern_result.get('total_score', 0)
            
            if three_tops_score < 2 or technical_score < 4:
                continue
            
            result = scoring_engine.score(
                symbol=symbol,
                oi_usd=oi_usd,
                total_volume_usd=cumulative_volume,
                funding_rate=funding_rate,
                three_tops_detected=pattern_result.get('three_tops', {}).get('detected', False),
                three_tops_score=three_tops_score,
                long_upper_shadow=pattern_result.get('long_upper_shadow', {}).get('detected', False),
                long_upper_shadow_score=pattern_result.get('long_upper_shadow', {}).get('score', 0),
                volume_divergence=pattern_result.get('volume_divergence', {}).get('detected', False),
                volume_divergence_score=pattern_result.get('volume_divergence', {}).get('score', 0),
                listing_hours=i,
                current_price=current_price,
                recent_coins_oi=[]
            )
            
            if result.total_score >= 6.5 and not result.veto:
                entry_count += 1
        
        if entry_count > 0:
            all_entry_points.append((symbol, entry_count, len(klines_1h)))

    print('=== 入场机会统计 ===')
    print(f'总币种数: {len(symbols)}')
    print(f'有入场机会的币种数: {len(all_entry_points)}')
    print(f'检查的K线总数: {total_klines_checked}')
    
    print(f'\n入场机会最多的前20个币种:')
    sorted_entries = sorted(all_entry_points, key=lambda x: x[1], reverse=True)
    for symbol, count, total_klines in sorted_entries[:20]:
        print(f'  {symbol}: {count}次入场机会 (共{total_klines}根K线)')

    total_entries = sum(c for _, c, _ in all_entry_points)
    print(f'\n总入场机会: {total_entries}次')
    print(f'平均每个币种: {total_entries/len(all_entry_points):.1f}次')


if __name__ == "__main__":
    main()
