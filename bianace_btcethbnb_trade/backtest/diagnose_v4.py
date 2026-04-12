#!/usr/bin/env python3
"""
v4 评分诊断工具
检查为什么所有信号都被过滤
"""

import json
from pathlib import Path
from datetime import datetime
import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.scoring_engine_v4 import get_scoring_engine_v4

def diagnose():
    # 加载数据
    data_file = project_root / 'data' / 'multi_timeframe_data.json'
    with open(data_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    scoring_engine = get_scoring_engine_v4()
    
    symbol = 'BTCUSDT'
    hourly = data[symbol].get('1h', [])
    
    print("诊断 v4 评分系统...\n")
    
    # 测试 10 个时间点
    test_count = 0
    veto_reasons = {}
    
    for i in range(100, len(hourly), 500):
        k = hourly[i]
        k_time = datetime.fromisoformat(k['timestamp'].replace('Z', '+00:00'))
        
        # 构建指标
        current_idx = {'1d': i//24, '4h': i//4, '1h': i}
        indicators = {}
        
        for tf in ['1d', '4h', '1h']:
            tf_data = data[symbol].get(tf, [])
            idx = current_idx[tf]
            if idx < 30:
                continue
            
            historical = tf_data[max(0, idx-100):idx+1]
            closes = [float(x['close']) for x in historical]
            highs = [float(x['high']) for x in historical]
            lows = [float(x['low']) for x in historical]
            volumes = [float(x['volume']) for x in historical]
            
            # 简化指标计算
            indicators[tf] = {
                'close': closes,
                'high': highs,
                'low': lows,
                'volume': volumes,
                'ema21': closes[-5:],  # 简化
                'ema50': closes[-5:],
                'rsi14': [50 + (i % 20) - 10],
                'atr14': [closes[-1] * 0.03],
                'macd': [0.01 * closes[-1]],
                'macd_signal': [0.005 * closes[-1]],
                'macd_hist': [0.005 * closes[-1]],
                'bb_upper': [closes[-1] * 1.05],
                'bb_middle': [closes[-1]],
                'bb_lower': [closes[-1] * 0.95],
                'adx': [20 + (i % 20)]  # 20-40 之间
            }
        
        # 构建数据
        data_snapshot = {
            'funding_rate': 0.0001,
            'price_change_24h': 0.02,
            'indicators': indicators
        }
        
        # 执行评分
        result = scoring_engine.score(symbol, data_snapshot)
        
        test_count += 1
        print(f"样本 {test_count}: {k_time}")
        print(f"  结果：分数={result['score']:.1f}, 等级={result['grade']}, 方向={result['direction']}")
        print(f"  市场状态：{result.get('market_state', 'N/A')}")
        print(f"  否决原因：{result.get('veto_reason', '无')}")
        if result.get('breakdown'):
            print(f"  维度评分：{result['breakdown']}")
        print()
        
        # 统计否决原因
        if result.get('veto_reason'):
            reason = result['veto_reason']
            veto_reasons[reason] = veto_reasons.get(reason, 0) + 1
    
    print("\n" + "="*60)
    print("否决原因统计:")
    for reason, count in sorted(veto_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}")
    
    print("\n诊断完成")

if __name__ == '__main__':
    diagnose()
