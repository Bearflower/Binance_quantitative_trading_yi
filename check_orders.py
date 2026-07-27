#!/usr/bin/env python3
"""检查订单状态"""
import asyncio
import os
from decimal import Decimal
import aiohttp
import hmac
import hashlib
from urllib.parse import urlencode

API_KEY = "dfeqPklQBFqdgOYHjCQEnPPwPxm1GVHVDd1VFmMMxh3pUHOIJ3UwSdRoDYuDWjL0"
API_SECRET = "K7X64Pdbawu15ACZzpZxyOokbULpNlPDbMPvhrAUC2z1n0CVc3zlC9r5PaaudDX5"
BASE_URL = "https://papi.binance.com"

async def request(session, method, endpoint, params=None, signed=True):
    if params is None:
        params = {}
    
    if signed:
        import time
        params['timestamp'] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            API_SECRET.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params['signature'] = signature
    
    url = f"{BASE_URL}{endpoint}"
    headers = {"X-MBX-APIKEY": API_KEY}
    
    async with session.request(method, url, params=params, headers=headers) as response:
        data = await response.json()
        return data

async def main():
    async with aiohttp.ClientSession() as session:
        print("\n" + "="*50)
        print("BTCUSDT 订单状态检查")
        print("="*50)
        
        print("\n【当前挂单】")
        orders = await request(session, "GET", "/papi/v1/um/openOrders", {"symbol": "BTCUSDT"})
        if orders:
            for o in orders:
                print(f"  订单ID: {o['orderId']}")
                print(f"  方向: {o['side']}")
                print(f"  类型: {o['type']}")
                print(f"  价格: {o.get('price', 'N/A')}")
                print(f"  数量: {o['origQty']}")
                print(f"  状态: {o['status']}")
                print("  ---")
        else:
            print("  无挂单")
        
        print("\n【当前持仓】")
        positions = await request(session, "GET", "/papi/v1/um/positionRisk", {"symbol": "BTCUSDT"})
        for p in positions:
            amt = float(p['positionAmt'])
            if amt != 0:
                print(f"  交易对: {p['symbol']}")
                print(f"  持仓量: {p['positionAmt']}")
                print(f"  入场价: {p['entryPrice']}")
                print(f"  未实现盈亏: {p['unRealizedProfit']}")
                print(f"  杠杆: {p['leverage']}")
        
        print("\n" + "="*50 + "\n")

if __name__ == "__main__":
    asyncio.run(main())
