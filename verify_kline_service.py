#!/usr/bin/env python3
"""
验证K线数据采集任务状态
"""

import requests
import json

# K线服务地址
KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"


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


def check_collection_tasks():
    """查询采集任务状态"""
    url = f"{KLINE_SERVICE_URL}/register/tasks/status"
    
    print(f"\n{'='*60}")
    print("查询采集任务状态...")
    print(f"{'='*60}")
    
    try:
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                data = result.get('data', {})
                total = data.get('total', 0)
                tasks = data.get('tasks', [])
                
                print(f"✅ 共有 {total} 个采集任务:")
                for task in tasks:
                    print(f"\n   任务ID: {task.get('task_id')}")
                    print(f"   交易对: {task.get('symbol')}")
                    print(f"   时间周期: {task.get('interval')}")
                    print(f"   下次运行: {task.get('next_run_time')}")
                
                return tasks
            else:
                print(f"❌ 查询失败: {result.get('message')}")
                return []
        else:
            print(f"❌ HTTP错误: {response.status_code}")
            return []
            
    except Exception as e:
        print(f"❌ 请求异常: {e}")
        return []


def check_kline_data(symbol: str, interval: str, limit: int = 10):
    """检查K线数据"""
    url = f"{KLINE_SERVICE_URL}/klines/latest"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    
    print(f"\n{'='*60}")
    print(f"检查 {symbol} {interval} K线数据...")
    print(f"{'='*60}")
    
    try:
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                data = result.get('data', [])
                
                print(f"✅ 获取到 {len(data)} 条K线数据:")
                if len(data) > 0:
                    # 显示最新的一条数据
                    latest = data[0]
                    print(f"\n   最新K线:")
                    print(f"   开盘时间: {latest.get('open_time')}")
                    print(f"   开盘价: {latest.get('open_price')}")
                    print(f"   最高价: {latest.get('high_price')}")
                    print(f"   最低价: {latest.get('low_price')}")
                    print(f"   收盘价: {latest.get('close_price')}")
                    print(f"   成交量: {latest.get('volume')}")
                
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
    print("\n" + "="*60)
    print("K线服务验证")
    print("="*60)
    
    # 1. 查询已注册的交易对
    symbols = check_registered_symbols()
    
    # 2. 查询采集任务状态
    tasks = check_collection_tasks()
    
    # 3. 检查新添加的交易对的K线数据
    print("\n" + "="*60)
    print("检查新添加交易对的K线数据")
    print("="*60)
    
    new_symbols = ["XRPUSDT", "TRXUSDT"]
    intervals = ["1h", "4h", "1d"]
    
    for symbol in new_symbols:
        for interval in intervals:
            check_kline_data(symbol, interval, limit=5)
    
    print("\n" + "="*60)
    print("✅ 验证完成！")
    print("="*60)
