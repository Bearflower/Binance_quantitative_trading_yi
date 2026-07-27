#!/usr/bin/env python3
"""设置止损订单 - 尝试多种订单类型"""
import asyncio
import aiohttp
import hmac
import hashlib
from urllib.parse import urlencode
import time
from decimal import Decimal, ROUND_DOWN

API_KEY = "dfeqPklQBFqdgOYHjCQEnPPwPxm1GVHVDd1VFmMMxh3pUHOIJ3UwSdRoDYuDWjL0"
API_SECRET = "K7X64Pdbawu15ACZzpZxyOokbULpNlPDbMPvhrAUC2z1n0CVc3zlC9r5PaaudDX5"
BASE_URL = "https://papi.binance.com"

async def request(session, method, endpoint, params=None, signed=True, base_url=None):
    if params is None:
        params = {}
    
    url_base = base_url if base_url else BASE_URL
    
    if signed:
        params['timestamp'] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            API_SECRET.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params['signature'] = signature
    
    url = f"{url_base}{endpoint}"
    headers = {"X-MBX-APIKEY": API_KEY}
    
    async with session.request(method, url, params=params, headers=headers) as response:
        data = await response.json()
        return data

async def main():
    symbol = "BTCUSDT"
    
    async with aiohttp.ClientSession() as session:
        print("\n" + "="*60)
        print("设置止损订单 - 尝试多种类型")
        print("="*60)
        
        # 获取当前价格
        ticker = await request(session, "GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol}, signed=False, base_url="https://fapi.binance.com")
        bid_price = Decimal(ticker['bidPrice'])
        ask_price = Decimal(ticker['askPrice'])
        print(f"当前买一价: {bid_price}")
        
        # 获取持仓
        positions = await request(session, "GET", "/papi/v1/um/positionRisk", {"symbol": symbol})
        for p in positions:
            amt = Decimal(p['positionAmt'])
            if amt != 0:
                position_amt = amt
                entry_price = Decimal(p['entryPrice'])
                print(f"持仓量: {position_amt}")
                print(f"入场价: {entry_price}")
        
        tick_size = Decimal("0.1")
        sl_price = (bid_price - tick_size * 4).quantize(tick_size, rounding=ROUND_DOWN)
        
        print(f"\n止损价格: {sl_price}")
        
        # 尝试1: STOP订单类型
        print(f"\n【尝试1: STOP类型】")
        sl_order = await request(session, "POST", "/papi/v1/um/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP",
            "quantity": str(abs(position_amt)),
            "price": str(sl_price),
            "stopPrice": str(sl_price)
        })
        print(f"结果: {sl_order}")
        
        if 'orderId' not in sl_order:
            # 尝试2: TAKE_PROFIT_MARKET
            print(f"\n【尝试2: TAKE_PROFIT_MARKET类型】")
            sl_order = await request(session, "POST", "/papi/v1/um/order", {
                "symbol": symbol,
                "side": "SELL",
                "type": "TAKE_PROFIT_MARKET",
                "quantity": str(abs(position_amt)),
                "stopPrice": str(sl_price)
            })
            print(f"结果: {sl_order}")
        
        if 'orderId' not in sl_order:
            # 尝试3: 使用普通限价单作为止损
            print(f"\n【尝试3: 普通限价止损单】")
            sl_order = await request(session, "POST", "/papi/v1/um/order", {
                "symbol": symbol,
                "side": "SELL",
                "type": "LIMIT",
                "quantity": str(abs(position_amt)),
                "price": str(sl_price),
                "timeInForce": "GTC",
                "reduceOnly": "true"
            })
            print(f"结果: {sl_order}")
        
        # 检查挂单
        print(f"\n【检查挂单】")
        orders = await request(session, "GET", "/papi/v1/um/openOrders", {"symbol": symbol})
        print(f"当前挂单数: {len(orders)}")
        for o in orders:
            print(f"  - {o['side']} {o['type']} @ {o.get('price', 'N/A')} stopPrice:{o.get('stopPrice', 'N/A')} (ID: {o['orderId']})")
        
        print("\n" + "="*60 + "\n")

if __name__ == "__main__":
    asyncio.run(main())
