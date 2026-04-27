#!/usr/bin/env python3
"""
测试新币做空系统接入通用 K 线服务

测试内容：
1. 从通用 K 线服务获取 K 线数据
2. 注册新币到 K 线服务
3. 查询已注册标的
4. 续期标的
5. 取消注册
"""

import sys
sys.path.insert(0, '/Users/yl/vscode/bianace_newtrade_trade/short_selling_system')

from core.binance_client import BinanceDataClient as BinanceClient


def test_get_klines():
    """测试获取 K 线数据"""
    print("=" * 80)
    print("测试 1: 从通用 K 线服务获取 K 线数据")
    print("=" * 80)
    
    client = BinanceClient()
    
    # 测试 BTCUSDT
    print("\n获取 BTCUSDT 15m K 线...")
    klines = client.get_kline_data("BTCUSDT", "15m", limit=5)
    
    if klines:
        print(f"✅ 成功获取 {len(klines)} 条 K 线数据")
        print(f"最新 K 线：open={klines[-1]['open']}, close={klines[-1]['close']}")
    else:
        print("❌ 获取 K 线数据失败")
    
    # 测试 ETHUSDT
    print("\n获取 ETHUSDT 1h K 线...")
    klines = client.get_kline_data("ETHUSDT", "1h", limit=3)
    
    if klines:
        print(f"✅ 成功获取 {len(klines)} 条 K 线数据")
        print(f"最新 K 线：open={klines[-1]['open']}, close={klines[-1]['close']}")
    else:
        print("❌ 获取 K 线数据失败")
    
    # 测试 BNBUSDT
    print("\n获取 BNBUSDT 4h K 线...")
    klines = client.get_kline_data("BNBUSDT", "4h", limit=2)
    
    if klines:
        print(f"✅ 成功获取 {len(klines)} 条 K 线数据")
        print(f"最新 K 线：open={klines[-1]['open']}, close={klines[-1]['close']}")
    else:
        print("❌ 获取 K 线数据失败")


def test_register_symbol():
    """测试注册新币"""
    print("\n" + "=" * 80)
    print("测试 2: 注册新币到 K 线服务")
    print("=" * 80)
    
    client = BinanceClient()
    
    # 测试注册一个虚拟新币
    test_symbol = "TESTUSDT"
    print(f"\n注册新币：{test_symbol}")
    success = client.register_new_symbol(
        symbol=test_symbol,
        intervals=["1m", "5m", "15m", "1h"],
        duration_days=7,
        priority="high"
    )
    
    if success:
        print(f"✅ {test_symbol} 注册成功")
    else:
        print(f"❌ {test_symbol} 注册失败（可能已存在）")


def test_query_registered():
    """测试查询已注册标的"""
    print("\n" + "=" * 80)
    print("测试 3: 查询已注册标的")
    print("=" * 80)
    
    import requests
    
    try:
        response = requests.get(
            "http://43.156.242.184:8765/api/v1/register",
            timeout=10
        )
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                data = result.get('data', [])
                print(f"\n已注册 {len(data)} 个标的：")
                for item in data:
                    symbol = item.get('symbol', 'unknown')
                    intervals = item.get('intervals', [])
                    expires = item.get('expires_at', 'unknown')
                    print(f"  - {symbol}: {len(intervals)} 个周期，过期时间：{expires}")
            else:
                print(f"❌ 查询失败：{result.get('message')}")
        else:
            print(f"❌ HTTP 错误：{response.status_code}")
            
    except Exception as e:
        print(f"❌ 查询异常：{e}")


def test_renew_symbol():
    """测试续期"""
    print("\n" + "=" * 80)
    print("测试 4: 续期已注册标的")
    print("=" * 80)
    
    client = BinanceClient()
    
    # 测试续期 BTCUSDT
    print("\n续期 BTCUSDT 3 天...")
    success = client.renew_symbol("BTCUSDT", 3)
    
    if success:
        print("✅ BTCUSDT 续期成功")
    else:
        print("❌ BTCUSDT 续期失败（可能未注册）")


def test_unregister_symbol():
    """测试取消注册"""
    print("\n" + "=" * 80)
    print("测试 5: 取消注册标的")
    print("=" * 80)
    
    client = BinanceClient()
    
    # 测试取消注册 TESTUSDT
    test_symbol = "TESTUSDT"
    print(f"\n取消注册：{test_symbol}")
    success = client.unregister_symbol(test_symbol)
    
    if success:
        print(f"✅ {test_symbol} 已取消注册")
    else:
        print(f"❌ {test_symbol} 取消失败")


def main():
    """主函数"""
    print("\n" + "=" * 80)
    print("新币做空系统 - 通用 K 线服务集成测试")
    print("=" * 80)
    
    # 测试 1: 获取 K 线数据
    test_get_klines()
    
    # 测试 2: 注册新币
    test_register_symbol()
    
    # 测试 3: 查询已注册标的
    test_query_registered()
    
    # 测试 4: 续期
    test_renew_symbol()
    
    # 测试 5: 取消注册
    test_unregister_symbol()
    
    print("\n" + "=" * 80)
    print("测试完成！")
    print("=" * 80)


if __name__ == "__main__":
    main()
