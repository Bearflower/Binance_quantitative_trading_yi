#!/usr/bin/env python3
"""设置止盈止损订单"""
import asyncio
import aiohttp
import hmac
import hashlib
from urllib.parse import urlencode
import time
from decimal import Decimal

API_KEY = "dfeqPklQBFqdgOYHjCQEnPPwPxm1GVHVDd1VFmMMxh3pUHOIJ3UwSdRoDYuDWjL0"
API_SECRET = "K7X64Pdbawu15ACZzpZxyOokbULpNlPDbMPvhrAUC2z1n0CVc3zlC9r5PaaudDX5"
BASE_URL = "https://papi.binance.com"

async def request(session, method, endpoint, params=None, signed=True):
    if params is None:
        params = {}
    
    if signed:
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
    symbol = "BTCUSDT"
    quantity = Decimal("0.003")
    entry_price = Decimal("80529.48")
    
    tick_size = Decimal("0.1")
    
    tp_price = entry_price + tick_size * 4
    sl_price = entry_price - tick_size * 4
    
    async with aiohttp.ClientSession() as session:
        print("\n" + "="*50)
        print("设置止盈止损订单")
        print("="*50)
        
        print(f"\n持仓信息:")
        print(f"  数量: {quantity} BTC")
        print(f"  入场价: {entry_price}")
        print(f"  止盈价: {tp_price} (+0.05%)")
        print(f"  止损价: {sl_price} (-0.05%)")
        
        print(f"\n【设置止盈订单】")
        tp_order = await request(session, "POST", "/papi/v1/um/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "LIMIT",
            "quantity": str(quantity),
            "price": str(tp_price),
            "timeInForce": "GTC",
            "reduceOnly": "true"
        })
        print(f"  结果: {tp_order}")
        
        print(f"\n【设置止损订单】")
        sl_order = await request(session, "POST", "/papi/v1/um/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP_MARKET",
            "quantity": str(quantity),
            "stopPrice": str(sl_price),
            "closePosition": "false"
        })
        print(f"  结果: {sl_order}")
        
        print("\n" + "="*50 + "\n")

if __name__ == "__main__":
    asyncio.run(main())
