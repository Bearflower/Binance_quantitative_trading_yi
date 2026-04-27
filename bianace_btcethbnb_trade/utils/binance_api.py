#!/usr/bin/env python3
"""
Binance API 数据获取模块
替代截图方法，直接从 API 获取交易数据和技术指标
"""

import requests
import json
import os
from datetime import datetime
from .technical_indicators import get_technical_indicators

# 通用 K 线服务配置
KLINE_SERVICE_URL = os.getenv('KLINE_SERVICE_URL', 'http://43.156.242.184:8765/api/v1')

def get_binance_futures_data(symbol="BTCUSDT"):
    """
    使用 Binance 期货 API 获取交易数据
    
    Args:
        symbol (str): 交易对符号，如 BTCUSDT
        
    Returns:
        dict: 包含交易数据的字典
    """
    # Binance 期货 API 端点
    url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}"
    
    try:
        # 发送请求
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            return response.json()
        else:
            print(f"API 请求失败：{response.status_code}")
            return None
            
    except Exception as e:
        print(f"API 请求错误：{str(e)}")
        return None

def get_orderbook_data(symbol="BTCUSDT", limit=5):
    """
    获取订单簿数据（用于 V6.13.2 限价单优化）
    
    Args:
        symbol (str): 交易对符号，如 BTCUSDT
        limit (int): 深度层级，默认 5（买一~买五，卖一~卖五）
        
    Returns:
        dict: 订单簿数据，包含 bids（买单）和 asks（卖单）
        示例：
        {
            "bids": [{"price": "95000.00", "qty": "0.5"}, ...],
            "asks": [{"price": "95000.50", "qty": "0.3"}, ...]
        }
    """
    url = f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit={limit}"
    
    try:
        response = requests.get(url, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            # 格式化订单簿数据
            orderbook = {
                'bids': [{'price': bid[0], 'qty': bid[1]} for bid in data.get('bids', [])],
                'asks': [{'price': ask[0], 'qty': ask[1]} for ask in data.get('asks', [])]
            }
            return orderbook
        else:
            print(f"订单簿 API 请求失败：{response.status_code}")
            return None
            
    except Exception as e:
        print(f"订单簿 API 请求错误：{str(e)}")
        return None

def get_multiple_symbols_data(symbols=["BTCUSDT", "ETHUSDT", "BNBUSDT"]):
    """
    获取多个交易对的数据
    
    Args:
        symbols (list): 交易对符号列表
        
    Returns:
        dict: 以交易对为键，数据为值的字典
    """
    data = {}
    
    for symbol in symbols:
        # 获取基本交易数据
        symbol_data = get_binance_futures_data(symbol)
        if symbol_data:
            # 获取技术指标数据（包含持仓量）
            indicators = get_technical_indicators(symbol)
            if indicators:
                symbol_data["indicators"] = indicators
            else:
                print(f"⚠️ 获取 {symbol} 技术指标数据失败")
                symbol_data["indicators"] = None
            data[symbol] = symbol_data
            print(f"✅ 获取 {symbol} 数据成功")
        else:
            print(f"❌ 获取 {symbol} 数据失败")
            # 即使基本数据失败，也尝试获取技术指标
            indicators = get_technical_indicators(symbol)
            if indicators:
                data[symbol] = {
                    "symbol": symbol,
                    "indicators": indicators,
                    "error": "Failed to fetch basic market data"
                }
            
    return data

def save_api_data(data, filename=None):
    """
    保存 API 数据到文件
    
    Args:
        data (dict): 要保存的数据
        filename (str): 文件名，默认自动生成
        
    Returns:
        str: 保存的文件路径
    """
    if not filename:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"binance_api_data_{timestamp}.json"
        
    filepath = f"./data/api_data/{filename}"
    
    # 确保目录存在
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        
    return filepath
