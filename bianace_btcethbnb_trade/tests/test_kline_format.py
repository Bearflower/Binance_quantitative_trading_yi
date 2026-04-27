#!/usr/bin/env python3
"""测试 K 线数据格式"""

import requests
import traceback

def test_kline_api():
    """测试 K 线 API 返回的数据格式"""
    print("=" * 60)
    print("测试 K 线 API")
    print("=" * 60)
    
    url = "http://43.156.242.184:8765/api/v1/klines/latest"
    params = {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "limit": 5
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            print(f"状态码：{response.status_code}")
            print(f"返回 code: {result.get('code')}")
            print(f"返回 message: {result.get('message')}")
            
            data = result.get('data', [])
            print(f"\ndata 类型：{type(data)}")
            print(f"data 长度：{len(data) if isinstance(data, list) else 'N/A'}")
            
            if isinstance(data, list) and len(data) > 0:
                print(f"\n第一个 K 线元素类型：{type(data[0])}")
                print(f"第一个 K 线元素内容:")
                for k, v in data[0].items():
                    print(f"  {k}: {v} (类型：{type(v)})")
                
                # 测试数据转换
                print("\n测试数据转换:")
                try:
                    close_prices = [float(k.get('close_price', 0)) for k in data]
                    print(f"✅ close_price 转换成功：{close_prices}")
                except Exception as e:
                    print(f"❌ close_price 转换失败：{e}")
                    traceback.print_exc()
                
                try:
                    high_prices = [float(k.get('high_price', 0)) for k in data]
                    print(f"✅ high_price 转换成功：{high_prices}")
                except Exception as e:
                    print(f"❌ high_price 转换失败：{e}")
                    traceback.print_exc()
                    
        else:
            print(f"HTTP 错误：{response.status_code}")
            
    except Exception as e:
        print(f"请求失败：{e}")
        traceback.print_exc()
    
    print("\n" + "=" * 60)

if __name__ == '__main__':
    test_kline_api()
