#!/usr/bin/env python3
"""
V4.1 回测脚本 - 调试版本
"""

import json
import sys
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'short_selling_system'))

from short_selling_system.core.scoring_engine_v41 import ScoringEngineV41, scoring_engine_v41
from short_selling_system.core.pattern_recognition_v4 import PatternRecognitionV4


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

def calculate_total_volume(klines: List[Dict]) -> float:
    """计算上线以来总交易量（USD）"""
    total_volume = 0.0
    for kline in klines:
        quote_volume = float(kline.get('quote_volume', 0))
        total_volume += quote_volume
    return total_volume

def calculate_atr(klines: List[Dict], period: int = 14) -> float:
    """计算ATR"""
    if len(klines) < period:
        period = len(klines)
    
    tr_list = []
    for i in range(1, len(klines)):
        high = float(klines[i]['high'])
        low = float(klines[i]['low'])
        prev_close = float(klines[i-1]['close'])
        
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    
    if len(tr_list) < period:
        return sum(tr_list) / len(tr_list) if tr_list else 0
    
    return sum(tr_list[-period:]) / period


def main():
    print("=" * 60)
    print("V4.1 回测调试")
    print("=" * 60)
    
    kline_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/2025_new_coins_data.json"
    real_data_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/real_oi_funding_data.json"
    
    print("\n加载数据...")
    kline_data = load_kline_data(kline_file)
    real_data = load_real_data(real_data_file)
    
    symbols = kline_data.get('metadata', {}).get('symbols', [])
    kline_data_dict = kline_data.get('data', {})
    print(f"共有 {len(symbols)} 个币种")
    
    scoring_engine = ScoringEngineV41()
    pattern_recognition = PatternRecognitionV4()
    
    valid_symbols = []
    for symbol in symbols[:20]:
        symbol_data = kline_data_dict.get(symbol, {})
        if not symbol_data:
            print(f"  {symbol}: 无K线数据")
            continue
        
        klines_1h = symbol_data.get('1h', {}).get('data', [])
        if not klines_1h:
            print(f"  {symbol}: 无1小时K线数据")
            continue
        
        real_info = real_data.get(symbol, {})
        oi_usd = real_info.get('oi_usd')
        funding_rate = real_info.get('funding_rate')
        
        if not oi_usd:
            print(f"  {symbol}: 无OI数据")
            continue
        
        total_volume = calculate_total_volume(klines_1h)
        oi_volume_ratio = oi_usd / total_volume if total_volume > 0 else 0
        
        print(f"  {symbol}: K线{len(klines_1h)}根, OI_USD=${oi_usd:,.0f}, 总交易量=${total_volume:,.0f}, OI/交易量={oi_volume_ratio:.4f}, 资金费率={funding_rate}")
        
        valid_symbols.append(symbol)
    
    print(f"\n有效币种: {len(valid_symbols)} 个")
    
    print("\n" + "=" * 60)
    print("检查第一个有效币种的评分过程")
    print("=" * 60)
    
    if valid_symbols:
        symbol = valid_symbols[0]
        symbol_data = kline_data_dict.get(symbol, {})
        klines_1h = symbol_data.get('1h', {}).get('data', [])
        real_info = real_data.get(symbol, {})
        
        oi_usd = real_info.get('oi_usd', 0)
        funding_rate = real_info.get('funding_rate', 0.00005)
        
        print(f"\n币种: {symbol}")
        print(f"OI_USD: ${oi_usd:,.0f}")
        print(f"资金费率: {funding_rate}")
        print(f"K线数量: {len(klines_1h)}")
        
        for i in range(20, min(30, len(klines_1h))):
            current_kline = klines_1h[i]
            current_price = float(current_kline['close'])
            current_time = datetime.fromtimestamp(current_kline['timestamp'] / 1000)
            listing_hours = i
            
            historical_klines = klines_1h[:i+1]
            cumulative_volume = calculate_total_volume(historical_klines)
            
            atr = calculate_atr(historical_klines, 14)
            
            pattern_result = pattern_recognition.analyze_patterns(historical_klines)
            
            three_tops_score = pattern_result.get('three_tops', {}).get('score', 0)
            technical_score = pattern_result.get('total_score', 0)
            
            print(f"\n  时间: {current_time}, 价格: {current_price:.6f}")
            print(f"  上线时间: {listing_hours}小时")
            print(f"  累计交易量: ${cumulative_volume:,.0f}")
            print(f"  ATR: {atr:.6f}")
            print(f"  三次冲顶得分: {three_tops_score}")
            print(f"  技术总分: {technical_score}")
            
            if three_tops_score >= 2 and technical_score >= 6:
                print(f"  ✓ 技术条件满足，计算评分...")
                
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
                    listing_hours=listing_hours,
                    current_price=current_price,
                    recent_coins_oi=[]
                )
                
                print(f"  合约分: {result.contract_score:.2f}")
                print(f"  技术分: {result.technical_score:.2f}")
                print(f"  情绪分: {result.sentiment_score:.2f}")
                print(f"  总分: {result.total_score:.2f}")
                print(f"  一票否决: {result.veto}, 原因: {result.veto_reason}")
                print(f"  OI/总交易量: {result.oi_volume_ratio:.4f}")
                
                if result.total_score >= 6.5 and not result.veto:
                    print(f"  ✓✓ 满足开仓条件！")
                    break
            else:
                print(f"  ✗ 技术条件不满足")


if __name__ == "__main__":
    main()
