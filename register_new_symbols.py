#!/usr/bin/env python3
"""
注册新的交易对到K线服务
"""

import requests
import json
from typing import List

# K线服务地址
KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"


def register_symbol(symbol: str, intervals: List[str], duration_days: int = 30, priority: str = "normal"):
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


def check_registered_symbols():
    """查询已注册的交易对"""
    url = f"{KLINE_SERVICE_URL}/register"
    
    print(f"\n{'='*60}")
    print("查询已注册的交易对...")
    print(f"{'='*60}")
    
    try:
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                data = result.get('data', [])
                total = result.get('total', 0)
                
                print(f"✅ 共有 {total} 个已注册的交易对:")
                for item in data:
                    print(f"\n   交易对: {item.get('symbol')}")
                    print(f"   时间周期: {item.get('intervals')}")
                    print(f"   状态: {item.get('status')}")
                    print(f"   过期时间: {item.get('expires_at')}")
                    print(f"   剩余天数: {item.get('duration_days', 0)} 天")
                
                return data
            else:
                print(f"❌ 查询失败: {result.get('message')}")
                return []
        else:
            print(f"❌ HTTP错误: {response.status_code}")
            return []
            
    except Exception as e:
        print(f"❌ 请求异常: {e}")
        return []


if __name__ == "__main__":
    # 需要添加的交易对
    symbols_to_add = [
        {
            "symbol": "XRPUSDT",
            "intervals": ["1h", "4h", "1d"],
            "duration_days": 30,
            "priority": "normal"
        },
        {
            "symbol": "SOLUSDT",
            "intervals": ["1h", "4h", "1d"],
            "duration_days": 30,
            "priority": "normal"
        },
        {
            "symbol": "TRXUSDT",
            "intervals": ["1h", "4h", "1d"],
            "duration_days": 30,
            "priority": "normal"
        }
    ]
    
    print("\n" + "="*60)
    print("开始注册新的交易对到K线服务")
    print("="*60)
    
    # 注册所有交易对
    success_count = 0
    for symbol_info in symbols_to_add:
        if register_symbol(**symbol_info):
            success_count += 1
    
    print(f"\n{'='*60}")
    print(f"注册完成！成功: {success_count}/{len(symbols_to_add)}")
    print(f"{'='*60}")
    
    # 查询已注册的交易对
    print("\n")
    check_registered_symbols()
