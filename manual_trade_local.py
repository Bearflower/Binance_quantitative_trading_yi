#!/usr/bin/env python3
"""
手动开仓脚本 - 本地执行
BTCUSDT合约：买一价开多，卖五价止盈，买五价止损
"""
import asyncio
import os
from decimal import Decimal
import aiohttp
import hmac
import hashlib
from urllib.parse import urlencode
import structlog

logger = structlog.get_logger()

API_KEY = "dfeqPklQBFqdgOYHjCQEnPPwPxm1GVHVDd1VFmMMxh3pUHOIJ3UwSdRoDYuDWjL0"
API_SECRET = "K7X64Pdbawu15ACZzpZxyOokbULpNlPDbMPvhrAUC2z1n0CVc3zlC9r5PaaudDX5"
BASE_URL = "https://papi.binance.com"


async def request(session, method, endpoint, params=None, signed=True):
    if params is None:
        params = {}
    
    if signed:
        params['timestamp'] = int(asyncio.get_event_loop().time() * 1000)
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
        if response.status != 200:
            print(f"API Error: {response.status} - {data}")
        return data


async def main():
    symbol = "BTCUSDT"
    leverage = 5
    margin_usdt = Decimal("50")
    
    async with aiohttp.ClientSession() as session:
        print(f"\n{'='*60}")
        print(f"BTCUSDT 手动开仓 (PM账户)")
        print(f"{'='*60}")
        
        print("\n步骤1: 获取当前市场价格...")
        async with session.get(f"https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol={symbol}") as resp:
            ticker = await resp.json()
        
        bid_price = Decimal(ticker['bidPrice'])
        ask_price = Decimal(ticker['askPrice'])
        
        print(f"  买一价: {bid_price}")
        print(f"  卖一价: {ask_price}")
        
        tick_size = Decimal("0.1")
        buy_5_price = bid_price - tick_size * 4
        sell_5_price = ask_price + tick_size * 4
        
        print(f"  买五价: {buy_5_price}")
        print(f"  卖五价: {sell_5_price}")
        
        print(f"\n步骤2: 设置杠杆倍数 {leverage}x...")
        leverage_result = await request(session, "POST", "/papi/v1/um/leverage", {
            "symbol": symbol,
            "leverage": leverage
        })
        print(f"  结果: {leverage_result}")
        
        print(f"\n步骤3: 计算开仓数量...")
        position_value = margin_usdt * leverage
        quantity = position_value / bid_price
        min_qty = Decimal("0.001")
        quantity = (quantity // min_qty) * min_qty
        
        print(f"  保证金: {margin_usdt} USDT")
        print(f"  杠杆: {leverage}x")
        print(f"  仓位价值: {position_value} USDT")
        print(f"  开仓数量: {quantity} BTC")
        print(f"  开仓价格: {bid_price}")
        
        print(f"\n步骤4: 执行开仓订单（限价买单）...")
        order = await request(session, "POST", "/papi/v1/um/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "LIMIT",
            "quantity": str(quantity),
            "price": str(bid_price),
            "timeInForce": "GTC"
        })
        print(f"  订单结果: {order}")
        
        if 'orderId' in order:
            print(f"\n步骤5: 设置止盈订单（卖五价）...")
            tp_order = await request(session, "POST", "/papi/v1/um/order", {
                "symbol": symbol,
                "side": "SELL",
                "type": "LIMIT",
                "quantity": str(quantity),
                "price": str(sell_5_price),
                "timeInForce": "GTC",
                "reduceOnly": "true"
            })
            print(f"  止盈订单: {tp_order}")
            
            print(f"\n步骤6: 设置止损订单（买五价触发）...")
            sl_order = await request(session, "POST", "/papi/v1/um/order", {
                "symbol": symbol,
                "side": "SELL",
                "type": "STOP_MARKET",
                "quantity": str(quantity),
                "stopPrice": str(buy_5_price),
                "closePosition": "false"
            })
            print(f"  止损订单: {sl_order}")
        
        print(f"\n{'='*60}")
        print("开仓完成!")
        print(f"{'='*60}")
        print(f"开仓价格: {bid_price}")
        print(f"止盈价格: {sell_5_price} (+{(sell_5_price - bid_price) / bid_price * 100:.2f}%)")
        print(f"止损价格: {buy_5_price} ({(buy_5_price - bid_price) / bid_price * 100:.2f}%)")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
