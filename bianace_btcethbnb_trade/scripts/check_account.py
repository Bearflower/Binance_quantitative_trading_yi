#!/usr/bin/env python3
"""检查账户信息和条件单"""
import sys
sys.path.insert(0, '/app')
from utils.binance_trade_api import BinanceTradeAPI
import requests

api = BinanceTradeAPI()

# 获取账户信息
url = f'{api.base_url}/papi/v1/account'
params = {'timestamp': int(__import__('time').time() * 1000)}
params['signature'] = api._generate_signature(__import__('urllib.parse').urlencode(params))
headers = api._get_headers()
r = requests.get(url, params=params, headers=headers)
data = r.json()

print('=== 账户信息 ===')
print(f"可用余额：{data.get('availableBalance', 'N/A')} USDT")
print(f"总余额：{data.get('totalWalletBalance', 'N/A')} USDT")
print()

# 检查条件单
print('=== 条件单检查 ===')
symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
total = 0
for symbol in symbols:
    url = f'{api.base_url}/papi/v1/um/conditional/order/get'
    params = {'symbol': symbol, 'timestamp': int(__import__('time').time() * 1000)}
    params['signature'] = api._generate_signature(__import__('urllib.parse').urlencode(params))
    r = requests.get(url, params=params, headers=headers)
    orders = r.json()
    if isinstance(orders, list):
        count = len(orders)
        total += count
        if count > 0:
            print(f"{symbol}: {count} 个条件单")
            for order in orders[:3]:  # 只显示前 3 个
                print(f"  - {order['orderId']}: {order['side']} {order['strategyType']} @ {order['stopPrice']}")
        else:
            print(f"{symbol}: 无条件单")
print(f"总计：{total} 个条件单")

if total > 8:
    print()
    print('⚠️  警告：条件单数量过多，可能导致 -1015 错误')
