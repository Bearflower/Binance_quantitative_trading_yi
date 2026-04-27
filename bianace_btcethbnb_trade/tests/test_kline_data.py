#!/usr/bin/env python3
"""快速测试 K 线数据处理"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data import MarketDataFetcher
import logging

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def test_kline_processing():
    """测试 K 线数据处理"""
    print("=" * 60)
    print("测试 K 线数据处理")
    print("=" * 60)
    
    fetcher = MarketDataFetcher()
    
    # 手动构造 K 线服务返回的数据格式
    test_data = {
        'BTCUSDT': {
            'klines': {
                '1h': [
                    {
                        'symbol': 'BTCUSDT',
                        'interval': '1h',
                        'open_time': 1776754800000,
                        'open_price': 75974.12,
                        'high_price': 75982.13,
                        'low_price': 75814.26,
                        'close_price': 75888.93,
                        'volume': 25.5272,
                    },
                    {
                        'symbol': 'BTCUSDT',
                        'interval': '1h',
                        'open_time': 1776762000000,
                        'open_price': 76507.21,
                        'high_price': 76587.82,
                        'low_price': 76385.31,
                        'close_price': 76411.64,
                        'volume': 108.15895,
                    }
                ] * 100  # 重复 100 次以足够计算指标
            },
            'symbol': 'BTCUSDT'
        }
    }
    
    try:
        # 处理数据
        processed = fetcher._process_api_data(test_data)
        
        if 'BTCUSDT' in processed:
            print("\n✅ 数据处理成功！")
            btc_data = processed['BTCUSDT']
            
            # 检查指标
            indicators = btc_data.get('indicators', {})
            for tf in ['1h']:
                if tf in indicators:
                    tf_data = indicators[tf]
                    print(f"\n{tf} 时间框架:")
                    print(f"  close: {tf_data.get('close')}")
                    print(f"  ema21: {tf_data.get('ema21')}")
                    print(f"  high: {tf_data.get('high')}")
                    print(f"  low: {tf_data.get('low')}")
                else:
                    print(f"\n{tf}: 无数据")
            
            print("\n" + "=" * 60)
            print("测试通过！")
            print("=" * 60)
            return True
        else:
            print("\n❌ 数据处理失败：未生成 BTCUSDT 数据")
            return False
            
    except Exception as e:
        print(f"\n❌ 处理失败：{e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    success = test_kline_processing()
    sys.exit(0 if success else 1)
