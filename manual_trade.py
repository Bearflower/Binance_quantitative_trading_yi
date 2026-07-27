#!/usr/bin/env python3
"""
手动开仓脚本
BTCUSDT合约：买一价开多，卖五价止盈，买五价止损
"""
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, '/app')

from shared.binance_api import BinanceClient
import structlog

logger = structlog.get_logger()


async def main():
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    
    client = BinanceClient(
        api_key=api_key,
        api_secret=api_secret,
        testnet=False,
        use_unified_account=True
    )
    
    async with client:
        symbol = "BTCUSDT"
        leverage = 5
        margin_usdt = Decimal("50")
        
        print(f"\n{'='*50}")
        print(f"BTCUSDT 手动开仓")
        print(f"{'='*50}")
        
        print("\n步骤1: 获取当前市场价格...")
        ticker = await client._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol}, signed=False)
        
        bid_price = Decimal(ticker['bidPrice'])
        ask_price = Decimal(ticker['askPrice'])
        bid_qty = Decimal(ticker['bidQty'])
        ask_qty = Decimal(ticker['askQty'])
        
        print(f"  买一价: {bid_price} (数量: {bid_qty})")
        print(f"  卖一价: {ask_price} (数量: {ask_qty})")
        
        tick_size = Decimal("0.1")
        buy_5_price = bid_price - tick_size * 4
        sell_5_price = ask_price + tick_size * 4
        
        print(f"  买五价: {buy_5_price}")
        print(f"  卖五价: {sell_5_price}")
        
        print(f"\n步骤2: 设置杠杆倍数 {leverage}x...")
        try:
            leverage_result = await client.set_leverage(symbol, leverage)
            print(f"  杠杆设置成功: {leverage_result}")
        except Exception as e:
            print(f"  杠杆设置结果: {e}")
        
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
        
        print(f"\n步骤4: 执行开仓订单（限价单）...")
        try:
            order = await client.place_order(
                symbol=symbol,
                side="BUY",
                quantity=quantity,
                price=bid_price,
                order_type="LIMIT",
                timeInForce="GTC"
            )
            print(f"  开仓订单成功!")
            print(f"  订单ID: {order['orderId']}")
            print(f"  状态: {order.get('status')}")
        except Exception as e:
            print(f"  开仓订单失败: {e}")
            return
        
        print(f"\n步骤5: 设置止盈订单（卖五价）...")
        try:
            tp_order = await client.place_order(
                symbol=symbol,
                side="SELL",
                quantity=quantity,
                price=sell_5_price,
                order_type="LIMIT",
                timeInForce="GTC",
                reduceOnly="true"
            )
            print(f"  止盈订单成功!")
            print(f"  订单ID: {tp_order['orderId']}")
            print(f"  止盈价格: {sell_5_price}")
        except Exception as e:
            print(f"  止盈订单失败: {e}")
        
        print(f"\n步骤6: 设置止损订单（买五价）...")
        try:
            sl_order = await client.place_order(
                symbol=symbol,
                side="SELL",
                quantity=quantity,
                stopPrice=buy_5_price,
                order_type="STOP_MARKET",
                closePosition="false"
            )
            print(f"  止损订单成功!")
            print(f"  订单ID: {sl_order['orderId']}")
            print(f"  止损价格: {buy_5_price}")
        except Exception as e:
            print(f"  止损订单失败: {e}")
        
        print(f"\n{'='*50}")
        print("开仓完成!")
        print(f"{'='*50}")
        print(f"开仓价格: {bid_price}")
        print(f"止盈价格: {sell_5_price} (+{(sell_5_price - bid_price) / bid_price * 100:.2f}%)")
        print(f"止损价格: {buy_5_price} ({(buy_5_price - bid_price) / bid_price * 100:.2f}%)")
        print(f"{'='*50}\n")


if __name__ == "__main__":
    asyncio.run(main())
