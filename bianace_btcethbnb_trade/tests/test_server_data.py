#!/usr/bin/env python3
"""在服务器上测试实际的数据流"""

import sys
import requests
sys.path.insert(0, '/root/bianace_btcethbnb_trade')

from core.data import MarketDataFetcher
import logging
import traceback

# 设置详细日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

def test_real_data():
    """测试真实数据流"""
    print("=" * 80)
    print("测试真实数据流 - 从 K 线服务获取数据")
    print("=" * 80)
    
    fetcher = MarketDataFetcher()
    
    try:
        # 1. 先从 K 线服务获取原始数据
        print("\n步骤 1: 从 K 线服务获取原始数据...")
        symbols = ['BTCUSDT']
        api_data = fetcher._fetch_from_kline_service(symbols)
        
        print(f"\n获取到的数据:")
        for symbol, data in api_data.items():
            print(f"\n{symbol}:")
            print(f"  数据类型：{type(data)}")
            print(f"  包含的键：{list(data.keys()) if isinstance(data, dict) else 'N/A'}")
            
            if 'klines' in data:
                klines = data['klines']
                print(f"  klines 类型：{type(klines)}")
                for tf in ['1d', '4h', '1h', '15m']:
                    if tf in klines:
                        tf_data = klines[tf]
                        print(f"    {tf}: {len(tf_data)} 条数据")
                        if tf_data:
                            print(f"      第一个元素类型：{type(tf_data[0])}")
                            if isinstance(tf_data[0], dict):
                                print(f"      第一个元素键：{list(tf_data[0].keys())}")
        
        # 2. 处理数据
        print("\n\n步骤 2: 处理数据...")
        processed = fetcher._process_api_data(api_data)
        
        print(f"\n处理后的数据:")
        for symbol, data in processed.items():
            print(f"\n{symbol}:")
            indicators = data.get('indicators', {})
            for tf in ['1d', '4h', '1h', '15m']:
                if tf in indicators:
                    tf_data = indicators[tf]
                    print(f"  {tf}:")
                    print(f"    close: {tf_data.get('close')}")
                    print(f"    ema21: {tf_data.get('ema21')}")
        
        print("\n" + "=" * 80)
        print("✅ 测试成功！")
        print("=" * 80)
        
    except Exception as e:
        print(f"\n❌ 测试失败：{e}")
        print(f"错误类型：{type(e).__name__}")
        print(f"错误详情：{str(e)}")
        traceback.print_exc()

if __name__ == '__main__':
    test_real_data()
