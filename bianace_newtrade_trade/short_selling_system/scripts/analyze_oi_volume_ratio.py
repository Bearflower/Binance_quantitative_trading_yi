#!/usr/bin/env python3
"""
分析OI/交易量比率分布
"""

import json
import sys
import os
from typing import Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'short_selling_system'))


def load_kline_data(file_path: str) -> Dict:
    """加载K线数据"""
    with open(file_path, 'r') as f:
        return json.load(f)

def load_real_data(file_path: str) -> Dict:
    """加载真实OI和资金费率数据"""
    with open(file_path, 'r') as f:
        data = json.load(f)
    
    result = {}
    for item in data['data']:
        symbol = item['symbol']
        result[symbol] = {
            'price': item.get('price'),
            'oi': item.get('oi'),
            'oi_usd': item.get('oi_usd'),
            'funding_rate': item.get('funding_rate'),
            'volume_24h': item.get('volume_24h')
        }
    return result

def calculate_total_volume(klines: List) -> float:
    """计算上线以来总交易量（USD）"""
    total_volume = 0.0
    for kline in klines:
        quote_volume = float(kline.get('quote_volume', 0))
        total_volume += quote_volume
    return total_volume


def main():
    print("=" * 60)
    print("分析OI/交易量比率分布")
    print("=" * 60)
    
    kline_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/2025_new_coins_data.json"
    real_data_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/real_oi_funding_data.json"
    
    kline_data = load_kline_data(kline_file)
    real_data = load_real_data(real_data_file)
    
    symbols = kline_data.get('metadata', {}).get('symbols', [])
    kline_data_dict = kline_data.get('data', {})
    
    ratios = []
    funding_rates = []
    
    for symbol in symbols:
        symbol_data = kline_data_dict.get(symbol, {})
        if not symbol_data:
            continue
        
        klines_1h = symbol_data.get('1h', {}).get('data', [])
        if not klines_1h:
            continue
        
        real_info = real_data.get(symbol, {})
        oi_usd = real_info.get('oi_usd')
        funding_rate = real_info.get('funding_rate')
        
        if not oi_usd:
            continue
        
        total_volume = calculate_total_volume(klines_1h)
        if total_volume <= 0:
            continue
        
        ratio = oi_usd / total_volume
        ratios.append(ratio)
        
        if funding_rate:
            funding_rates.append(funding_rate)
    
    print(f"\n共分析了 {len(ratios)} 个币种")
    
    ratios_sorted = sorted(ratios)
    
    print(f"\n=== OI/交易量比率分布 ===")
    print(f"最小值: {min(ratios):.6f}")
    print(f"最大值: {max(ratios):.6f}")
    print(f"平均值: {sum(ratios)/len(ratios):.6f}")
    print(f"中位数: {ratios_sorted[len(ratios)//2]:.6f}")
    
    percentiles = [10, 25, 50, 75, 90, 95, 99]
    print(f"\n分位数分布:")
    for p in percentiles:
        idx = int(len(ratios_sorted) * p / 100)
        if idx >= len(ratios_sorted):
            idx = len(ratios_sorted) - 1
        print(f"  {p}%分位: {ratios_sorted[idx]:.6f}")
    
    print(f"\n=== 资金费率分布 ===")
    if funding_rates:
        funding_sorted = sorted(funding_rates)
        print(f"最小值: {min(funding_rates):.6f}")
        print(f"最大值: {max(funding_rates):.6f}")
        print(f"平均值: {sum(funding_rates)/len(funding_rates):.6f}")
        print(f"中位数: {funding_sorted[len(funding_rates)//2]:.6f}")
        
        print(f"\n分位数分布:")
        for p in percentiles:
            idx = int(len(funding_sorted) * p / 100)
            if idx >= len(funding_sorted):
                idx = len(funding_sorted) - 1
            print(f"  {p}%分位: {funding_sorted[idx]:.6f}")
    
    print(f"\n=== 建议的评分阈值 ===")
    p75 = ratios_sorted[int(len(ratios_sorted) * 0.75)]
    p90 = ratios_sorted[int(len(ratios_sorted) * 0.90)]
    p95 = ratios_sorted[int(len(ratios_sorted) * 0.95)]
    
    print(f"基于分位数建议:")
    print(f"  比率 > {p95:.4f} (95%分位): 极高，10分")
    print(f"  比率 > {p90:.4f} (90%分位): 高，7分")
    print(f"  比率 > {p75:.4f} (75%分位): 中等，4分")
    print(f"  比率 <= {p75:.4f}: 低，1分")


if __name__ == "__main__":
    main()
