#!/usr/bin/env python3
"""
重试注册SOLUSDT交易对
"""

import requests
import json

# K线服务地址
KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"


def register_symbol(symbol: str, intervals: list, duration_days: int = 30, priority: str = "normal"):
    """
    注册新的交易对
    
    Args:
        symbol: 交易对符号
        intervals: 时间周期列表
        duration_days: 持续天数
        priority: 优先级
    """
    url = f"{KLINE_SERVICE_URL}/register"
    
    payload = {
        "symbol": symbol,
        "intervals": intervals,
        "duration_days": duration_days,
        "priority": priority
    }
    
    print(f"\n{'='*60}")
    print(f"正在注册交易对: {symbol}")
    print(f"时间周期: {intervals}")
    print(f"持续天数: {duration_days}")
    print(f"优先级: {priority}")
    print(f"{'='*60}")
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                data = result.get('data', {})
                print(f"✅ 注册成功！")
                print(f"   交易对: {data.get('symbol')}")
                print(f"   时间周期: {data.get('intervals')}")
                print(f"   注册时间: {data.get('registered_at')}")
                print(f"   过期时间: {data.get('expires_at')}")
                print(f"   状态: {data.get('status')}")
                return True
            else:
                print(f"❌ 注册失败: {result.get('message')}")
                return False
        else:
            print(f"❌ HTTP错误: {response.status_code}")
            print(f"   响应内容: {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ 请求异常: {e}")
        return False


if __name__ == "__main__":
    # 重试注册SOLUSDT
    success = register_symbol(
        symbol="SOLUSDT",
        intervals=["1h", "4h", "1d"],
        duration_days=30,
        priority="normal"
    )
    
    if success:
        print("\n✅ SOLUSDT 注册成功！")
    else:
        print("\n❌ SOLUSDT 注册失败，可能需要检查服务器日志")
