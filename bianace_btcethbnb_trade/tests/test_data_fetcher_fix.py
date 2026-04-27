#!/usr/bin/env python3
"""
测试 data_fetcher 修复效果
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data import MarketDataFetcher
import logging

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def test_fetch_data():
    """测试数据获取"""
    print("=" * 80)
    print("测试行情数据获取")
    print("=" * 80)
    
    fetcher = MarketDataFetcher()
    
    try:
        print("\n开始获取行情数据...")
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
        data = fetcher.fetch_market_data(symbols)
        
        if data:
            print(f"\n✅ 成功获取 {len(data)} 个交易对的数据")
            
            for symbol, symbol_data in data.items():
                print(f"\n{symbol}:")
                print(f"  last_price: {symbol_data.get('last_price')}")
                print(f"  price_change_24h: {symbol_data.get('price_change_24h')}")
                print(f"  funding_rate: {symbol_data.get('funding_rate')}")
                
                indicators = symbol_data.get('indicators', {})
                print(f"  indicators 时间框架：{list(indicators.keys())}")
                
                for tf in ['1d', '4h', '1h', '15m']:
                    if tf in indicators and indicators[tf]:
                        tf_data = indicators[tf]
                        print(f"    {tf}:")
                        print(f"      close: {tf_data.get('close')}")
                        print(f"      ema21: {tf_data.get('ema21')}")
                        print(f"      rsi: {tf_data.get('rsi')}")
                        print(f"      atr14: {tf_data.get('atr14')}")
                    else:
                        print(f"    {tf}: 无数据")
            
            print("\n" + "=" * 80)
            print("✅ 测试通过！")
            print("=" * 80)
            return True
        else:
            print("\n❌ 获取数据失败：返回为空")
            return False
            
    except Exception as e:
        import traceback
        print(f"\n❌ 测试失败：{e}")
        print(f"错误类型：{type(e).__name__}")
        traceback.print_exc()
        return False

if __name__ == '__main__':
    success = test_fetch_data()
    sys.exit(0 if success else 1)
