#!/usr/bin/env python3
"""
评分分布诊断工具
分析 v2 评分引擎返回的分数分布，帮助确定合适的阈值
"""

import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import sys

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.scoring_engine_v2 import get_scoring_engine_v2

def diagnose():
    # 加载数据
    data_file = project_root / 'data' / 'multi_timeframe_data.json'
    with open(data_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    scoring_engine = get_scoring_engine_v2()
    
    # 收集所有评分
    all_scores = []
    grade_distribution = defaultdict(int)
    score_ranges = defaultdict(int)
    
    symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
    total_signals = 0
    
    print("开始分析评分分布...\n")
    
    for symbol in symbols:
        if symbol not in data:
            continue
        
        hourly = data[symbol].get('1h', [])
        print(f"分析 {symbol} ({len(hourly)} 根 1h K 线)...")
        
        # 每隔 100 根 K 线采样一次
        for i in range(0, len(hourly), 100):
            k = hourly[i]
            k_time = datetime.fromisoformat(k['timestamp'].replace('Z', '+00:00'))
            
            # 构建数据快照
            data_snapshot = {
                '1h': {'data': hourly[max(0, i-55):i+1]},
                '4h': {'data': data[symbol].get('4h', [])[max(0, i//4-55):i//4+1]},
                '1d': {'data': data[symbol].get('1d', [])[max(0, i//24-55):i//24+1]}
            }
            
            # 执行评分
            score_result = scoring_engine.score(symbol, data_snapshot)
            
            if score_result and score_result['grade']:
                total_signals += 1
                score = score_result['score']
                grade = score_result['grade']
                direction = score_result.get('direction', 'N/A')
                
                all_scores.append(score)
                grade_distribution[grade] += 1
                
                # 分数段统计
                if score >= 90:
                    score_ranges['90+'] += 1
                elif score >= 85:
                    score_ranges['85-89'] += 1
                elif score >= 80:
                    score_ranges['80-84'] += 1
                elif score >= 75:
                    score_ranges['75-79'] += 1
                elif score >= 70:
                    score_ranges['70-74'] += 1
                elif score >= 65:
                    score_ranges['65-69'] += 1
                elif score >= 60:
                    score_ranges['60-64'] += 1
                else:
                    score_ranges['<60'] += 1
                
                # 打印前 10 个样本
                if len(all_scores) <= 10:
                    print(f"  样本：{k_time}, 分数={score:.1f}, 等级={grade}, 方向={direction}")
    
    # 统计分析
    print("\n" + "="*60)
    print("评分分布统计")
    print("="*60)
    
    if all_scores:
        avg_score = sum(all_scores) / len(all_scores)
        min_score = min(all_scores)
        max_score = max(all_scores)
        
        print(f"\n总信号数：{total_signals}")
        print(f"平均分数：{avg_score:.2f}")
        print(f"最低分数：{min_score:.2f}")
        print(f"最高分数：{max_score:.2f}")
        
        print("\n等级分布:")
        for grade in ['S', 'A', 'B', 'C']:
            count = grade_distribution[grade]
            pct = count / total_signals * 100 if total_signals > 0 else 0
            print(f"  {grade}级：{count} ({pct:.1f}%)")
        
        print("\n分数段分布:")
        for range_name in ['90+', '85-89', '80-84', '75-79', '70-74', '65-69', '60-64', '<60']:
            count = score_ranges[range_name]
            pct = count / total_signals * 100 if total_signals > 0 else 0
            bar = '█' * int(pct / 2)
            print(f"  {range_name:8} {count:4} ({pct:5.1f}%) {bar}")
        
        print("\n" + "="*60)
        print("建议阈值设置:")
        print("="*60)
        
        # 计算不同阈值的信号数量
        for threshold in [85, 80, 75, 70, 65, 60]:
            count = sum(1 for s in all_scores if s >= threshold)
            pct = count / total_signals * 100 if total_signals > 0 else 0
            print(f"  阈值≥{threshold}分：{count} 信号 ({pct:.1f}%)")
        
        print("\n💡 建议:")
        if avg_score < 75:
            print("  当前评分系统返回分数偏低，建议:")
            print("  1. 将 S 级阈值设为 75 分（而非 80 分）")
            print("  2. 将 A 级阈值设为 65 分（而非 70 分）")
            print("  3. 或者调整评分引擎的权重/算法提高分数")
        else:
            print("  当前评分系统返回分数合理，可以使用 80/70 阈值")

if __name__ == '__main__':
    diagnose()
