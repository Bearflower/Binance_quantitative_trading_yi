#!/usr/bin/env python3
"""
从币安获取回测所需的多维度数据
支持：K 线数据、资金费率、持仓量 (OI)
目标币种：PRLUSDT, NATGASUSDT, BAUSDT, BZUSDT, CLUSDT, BASEUSDT, 
         GOOGLUSDT, NVDAUSDT, METAUSDT, XAUTUSDT, BSBUSDT, PAYPUSDT
"""

import requests
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict

TARGET_SYMBOLS = [
    'PRLUSDT', 'NATGASUSDT', 'BAUSDT', 'BZUSDT',
    'CLUSDT', 'BASEUSDT', 'GOOGLUSDT', 'NVDAUSDT',
    'METAUSDT', 'XAUTUSDT', 'BSBUSDT', 'PAYPUSDT'
]


def fetch_klines(symbol: str, interval: str = '1h', limit: int = 5000) -> List[Dict]:
    """从币安获取 K 线数据"""
    url = 'https://fapi.binance.com/fapi/v1/klines'
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if not isinstance(data, list):
            print(f"  ⚠️  {symbol} {interval} 数据获取失败：{data}")
            return []
        
        klines = []
        for k in data:
            kline = {
                'timestamp': datetime.fromtimestamp(k[0] / 1000).isoformat(),
                'open': str(k[1]),
                'high': str(k[2]),
                'low': str(k[3]),
                'close': str(k[4]),
                'volume': str(k[5])
            }
            klines.append(kline)
        
        return klines
    except Exception as e:
        print(f"  ❌ {symbol} {interval} 请求异常：{e}")
        return []


def fetch_funding_rate(symbol: str, limit: int = 1000) -> List[Dict]:
    """从币安获取资金费率历史"""
    url = 'https://fapi.binance.com/fapi/v1/fundingRate'
    params = {
        'symbol': symbol,
        'limit': limit
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if not isinstance(data, list):
            return []
        
        rates = []
        for item in data:
            rate = {
                'timestamp': datetime.fromtimestamp(item['fundingTime'] / 1000).isoformat(),
                'fundingRate': str(item['fundingRate'])
            }
            rates.append(rate)
        
        return rates
    except Exception as e:
        print(f"  ⚠️  {symbol} 资金费率获取失败：{e}")
        return []


def fetch_open_interest(symbol: str) -> Dict:
    """获取当前持仓量 (OI)"""
    url = 'https://fapi.binance.com/fapi/v1/openInterest'
    params = {'symbol': symbol}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if 'openInterest' in data:
            return {
                'symbol': symbol,
                'openInterest': str(data['openInterest']),
                'timestamp': datetime.now().isoformat()
            }
        return {}
    except Exception as e:
        print(f"  ⚠️  {symbol} OI 获取失败：{e}")
        return {}


def fetch_symbol_info(symbol: str) -> Dict:
    """获取合约基本信息"""
    url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
    
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        
        for s in data.get('symbols', []):
            if s['symbol'] == symbol:
                return {
                    'symbol': symbol,
                    'contractType': s.get('contractType', 'UNKNOWN'),
                    'listTime': s.get('listTime'),
                    'baseAsset': s.get('baseAsset', ''),
                    'quoteAsset': s.get('quoteAsset', ''),
                }
        return {}
    except Exception as e:
        print(f"  ⚠️  {symbol} 信息获取失败：{e}")
        return {}


def fetch_multi_timeframe_data(symbols: List[str], days: int = 180) -> Dict:
    """获取多时间框架数据"""
    klines_1d = days
    klines_4h = days * 6
    klines_1h = days * 24
    
    data = {}
    
    for symbol in symbols:
        print(f"\n获取 {symbol} 数据...")
        
        symbol_data = {
            'symbol_info': fetch_symbol_info(symbol),
            '1d': fetch_klines(symbol, interval='1d', limit=klines_1d),
            '4h': fetch_klines(symbol, interval='4h', limit=klines_4h),
            '1h': fetch_klines(symbol, interval='1h', limit=klines_1h),
            'funding_rate': fetch_funding_rate(symbol, limit=days * 3),
            'open_interest': fetch_open_interest(symbol)
        }
        
        print(f"  ✅ 1d: {len(symbol_data['1d'])} 条")
        print(f"  ✅ 4h: {len(symbol_data['4h'])} 条")
        print(f"  ✅ 1h: {len(symbol_data['1h'])} 条")
        print(f"  ✅ 资金费率：{len(symbol_data['funding_rate'])} 条")
        
        data[symbol] = symbol_data
        time.sleep(0.5)
    
    return data


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='获取币安回测数据')
    parser.add_argument('--symbols', type=str, default=','.join(TARGET_SYMBOLS))
    parser.add_argument('--days', type=int, default=180)
    parser.add_argument('--output', type=str, default='data/backtest_data.json')
    
    args = parser.parse_args()
    
    symbols = [s.strip() for s in args.symbols.split(',')]
    
    print("=" * 80)
    print("币安回测数据获取工具")
    print("=" * 80)
    print(f"目标币种：{len(symbols)} 个")
    for s in symbols:
        print(f"  - {s}")
    print(f"数据天数：{args.days} 天")
    print("=" * 80)
    
    start_time = time.time()
    
    data = fetch_multi_timeframe_data(symbols, days=args.days)
    
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    elapsed = time.time() - start_time
    
    print("\n" + "=" * 80)
    print(f"✅ 数据已保存到：{output_path}")
    
    total_klines = sum(
        len(d['1d']) + len(d['4h']) + len(d['1h'])
        for d in data.values()
    )
    print(f"📊 总 K 线数：{total_klines} 条")
    print(f"⏱️  耗时：{elapsed:.1f} 秒")
    print("=" * 80)


if __name__ == '__main__':
    main()
