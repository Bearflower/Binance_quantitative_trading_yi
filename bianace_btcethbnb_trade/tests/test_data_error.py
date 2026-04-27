#!/usr/bin/env python3
"""测试数据流，找出错误"4"的具体位置"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data import MarketDataFetcher
import logging
import traceback

# 设置日志
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

def test_data_processing():
    """测试数据处理流程"""
    print("=" * 80)
    print("测试数据处理流程")
    print("=" * 80)
    
    # 模拟 K 线服务返回的数据
    test_api_data = {
        'BTCUSDT': {
            'klines': {
                '1d': [{'close_price': 75000, 'high_price': 76000, 'low_price': 74000, 'open_price': 74500, 'volume': 1000}],
                '4h': [{'close_price': 75500, 'high_price': 76000, 'low_price': 75000, 'open_price': 75200, 'volume': 500}],
                '1h': [{'close_price': 75614.42, 'high_price': 75614.42, 'low_price': 75474.77, 'open_price': 75581.33, 'volume': 65.95848}],
                '15m': [{'close_price': 75700, 'high_price': 75800, 'low_price': 75600, 'open_price': 75650, 'volume': 100}]
            },
            'symbol': 'BTCUSDT',
            'lastPrice': '75614.42',
            'priceChangePercent': '0',
            'funding_rate': '0'
        }
    }
    
    fetcher = MarketDataFetcher()
    
    try:
        print("\n开始处理数据...")
        processed = fetcher._process_api_data(test_api_data)
        
        if 'BTCUSDT' in processed:
            print("\n✅ 数据处理成功！")
            btc_data = processed['BTCUSDT']
            
            # 检查指标
            indicators = btc_data.get('indicators', {})
            for tf in ['1d', '4h', '1h', '15m']:
                if tf in indicators:
                    tf_data = indicators[tf]
                    print(f"\n{tf} 时间框架:")
                    print(f"  close: {tf_data.get('close')}")
                    print(f"  ema21: {tf_data.get('ema21')}")
                    print(f"  rsi: {tf_data.get('rsi')}")
                    print(f"  atr14: {tf_data.get('atr14')}")
                else:
                    print(f"\n{tf}: 无数据")
            
            print("\n" + "=" * 80)
            print("测试通过！")
            print("=" * 80)
            return True
        else:
            print("\n❌ 数据处理失败：未生成 BTCUSDT 数据")
            return False
            
    except Exception as e:
        print(f"\n❌ 处理失败：{e}")
        print(f"错误类型：{type(e).__name__}")
        print(f"错误详情：{str(e)}")
        traceback.print_exc()
        return False

if __name__ == '__main__':
    success = test_data_processing()
    sys.exit(0 if success else 1)
