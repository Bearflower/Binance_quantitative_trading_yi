#!/usr/bin/env python3
"""设置止盈止损订单 - 修正版"""
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
        print("设置止盈止损订单")
        print("="*60)
        
        # 获取交易规则
        print("\n步骤1: 获取交易规则...")
        exchange_info = await request(session, "GET", "/fapi/v1/exchangeInfo", {"symbol": symbol}, signed=False, base_url="https://fapi.binance.com")
        
        for f in exchange_info['symbols'][0]['filters']:
            if f['filterType'] == 'PRICE_FILTER':
                tick_size = Decimal(f['tickSize'])
                print(f"  价格精度: {tick_size}")
            elif f['filterType'] == 'LOT_SIZE':
                step_size = Decimal(f['stepSize'])
                print(f"  数量精度: {step_size}")
        
        # 获取当前价格
        print("\n步骤2: 获取当前价格...")
        ticker = await request(session, "GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol}, signed=False, base_url="https://fapi.binance.com")
        bid_price = Decimal(ticker['bidPrice'])
        ask_price = Decimal(ticker['askPrice'])
        print(f"  买一价: {bid_price}")
        print(f"  卖一价: {ask_price}")
        
        # 获取持仓
        print("\n步骤3: 获取当前持仓...")
        positions = await request(session, "GET", "/papi/v1/um/positionRisk", {"symbol": symbol})
        for p in positions:
            amt = Decimal(p['positionAmt'])
            if amt != 0:
                position_amt = amt
                entry_price = Decimal(p['entryPrice'])
                print(f"  持仓量: {position_amt}")
                print(f"  入场价: {entry_price}")
        
        # 计算止盈止损价格（卖五价止盈，买五价止损）
        tp_price = (ask_price + tick_size * 4).quantize(tick_size, rounding=ROUND_DOWN)
        sl_price = (bid_price - tick_size * 4).quantize(tick_size, rounding=ROUND_DOWN)
        
        print(f"\n步骤4: 设置止盈止损...")
        print(f"  止盈价格: {tp_price}")
        print(f"  止损价格: {sl_price}")
        
        # 设置止盈订单
        print(f"\n【设置止盈订单 - 限价卖单】")
        tp_order = await request(session, "POST", "/papi/v1/um/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "LIMIT",
            "quantity": str(abs(position_amt)),
            "price": str(tp_price),
            "timeInForce": "GTC",
            "reduceOnly": "true"
        })
        if 'orderId' in tp_order:
            print(f"  ✅ 止盈订单成功! ID: {tp_order['orderId']}")
        else:
            print(f"  ❌ 止盈订单失败: {tp_order}")
        
        # 设置止损订单
        print(f"\n【设置止损订单 - 止损市价单】")
        sl_order = await request(session, "POST", "/papi/v1/um/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP",
            "quantity": str(abs(position_amt)),
            "price": str(sl_price),
            "stopPrice": str(sl_price),
            "timeInForce": "GTC"
        })
        if 'orderId' in sl_order:
            print(f"  ✅ 止损订单成功! ID: {sl_order['orderId']}")
        else:
            print(f"  ❌ 止损订单失败: {sl_order}")
        
        # 检查挂单
        print(f"\n步骤5: 检查挂单...")
        orders = await request(session, "GET", "/papi/v1/um/openOrders", {"symbol": symbol})
        print(f"  当前挂单数: {len(orders)}")
        for o in orders:
            print(f"    - {o['side']} {o['type']} @ {o.get('price', 'N/A')} (ID: {o['orderId']})")
        
        print("\n" + "="*60 + "\n")

if __name__ == "__main__":
    asyncio.run(main())
