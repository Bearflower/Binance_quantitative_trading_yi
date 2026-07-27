#!/usr/bin/env python3
"""
检查 BTCUSDT 的止盈止损条件单，验证是否缺少 reduceOnly / closePosition 保护
"""
import asyncio
import aiohttp
import hmac
import hashlib
from urllib.parse import urlencode
import time
import os
import sys
from decimal import Decimal

# 从环境变量读取 API 密钥
API_KEY = os.getenv("BINANCE_API_KEY", "dfeqPklQBFqdgOYHjCQEnPPwPxm1GVHVDd1VFmMMxh3pUHOIJ3UwSdRoDYuDWjL0")
API_SECRET = os.getenv("BINANCE_API_SECRET", "K7X64Pdbawu15ACZzpZxyOokbULpNlPDbMPvhrAUC2z1n0CVc3zlC9r5PaaudDX5")
BASE_URL = "https://papi.binance.com"
FUTURES_URL = "https://fapi.binance.com"

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
    print("=" * 70)
    print("  BTCUSDT 止盈止损条件单检查")
    print("=" * 70)
    
    async with aiohttp.ClientSession() as session:
        # 1. 获取当前价格
        print("\n【1】当前行情")
        ticker = await request(session, "GET", "/fapi/v1/ticker/bookTicker",
                               {"symbol": "BTCUSDT"}, signed=False,
                               base_url=FUTURES_URL)
        print(f"  买一价: {ticker.get('bidPrice')}")
        print(f"  卖一价: {ticker.get('askPrice')}")

        # 2. 获取持仓
        print("\n【2】当前持仓")
        positions = await request(session, "GET", "/papi/v1/um/positionRisk",
                                  {"symbol": "BTCUSDT"})
        found = False
        for p in positions:
            amt = float(p.get('positionAmt', '0'))
            if abs(amt) > 0:
                found = True
                direction = "多仓 LONG" if amt > 0 else "空仓 SHORT"
                print(f"  方向: {direction}")
                print(f"  数量: {abs(amt)}")
                print(f"  入场价: {p.get('entryPrice')}")
                print(f"  标记价: {p.get('markPrice')}")
                print(f"  未实现盈亏: {p.get('unRealizedProfit')}")
        if not found:
            print("  无持仓")

        # 3. 获取普通挂单（检测是否有非 reduceOnly 的止盈止损单）
        print("\n【3】普通挂单")
        try:
            open_orders = await request(session, "GET", "/papi/v1/um/openOrders",
                                        {"symbol": "BTCUSDT"})
            if open_orders and len(open_orders) > 0:
                print(f"  共 {len(open_orders)} 个挂单:")
                for o in open_orders:
                    has_reduce = o.get('reduceOnly', False)
                    has_close = o.get('closePosition', False)
                    status = "✅ 有保护" if (has_reduce or has_close) else "⚠️ 无保护！"
                    print(f"    ID={o.get('orderId')} | {o.get('side')} {o.get('type')} "
                          f"| 数量={o.get('origQty')} | 价格={o.get('price', '-')} "
                          f"| reduceOnly={has_reduce} closePosition={has_close} {status}")
            else:
                print("  无挂单")
        except Exception as e:
            print(f"  查询失败: {e}")

        # 4. 获取条件单（算法单）- 这是关键！
        print("\n【4】条件单（算法单/algo orders）")
        try:
            algo_orders = await request(session, "GET", "/papi/v1/um/algo/openOrderList",
                                        {"symbol": "BTCUSDT"})
            if algo_orders and len(algo_orders) > 0:
                print(f"  共 {len(algo_orders)} 个条件单:")
                for o in algo_orders:
                    # 条件单的响应结构
                    algo_id = o.get('algoId', 'N/A')
                    symbol = o.get('symbol', 'N/A')
                    side = o.get('side', 'N/A')
                    order_type = o.get('type', 'N/A')
                    quantity = o.get('quantity', 'N/A')
                    trigger_price = o.get('triggerPrice', o.get('stopPrice', 'N/A'))
                    # 检查是否有保护参数
                    has_reduce = o.get('reduceOnly', False)
                    has_close = o.get('closePosition', False)
                    status = "✅ 有保护" if (has_reduce or has_close) else "⚠️ 无保护！"
                    print(f"    algoId={algo_id} | {side} {order_type} "
                          f"| 数量={quantity} | 触发价={trigger_price} "
                          f"| reduceOnly={has_reduce} closePosition={has_close} {status}")
            else:
                print("  无条件单")
        except Exception as e:
            print(f"  查询条件单失败: {e}")
            # 尝试用另一种方式查询
            print("\n  尝试备用查询方式...")
            try:
                algo_orders_v2 = await request(session, "GET", "/papi/v1/algo/openOrderList",
                                               {"symbol": "BTCUSDT"})
                if algo_orders_v2 and len(algo_orders_v2) > 0:
                    print(f"  共 {len(algo_orders_v2)} 个条件单:")
                    for o in algo_orders_v2:
                        algo_id = o.get('algoId', 'N/A')
                        side = o.get('side', 'N/A')
                        order_type = o.get('type', 'N/A')
                        quantity = o.get('quantity', 'N/A')
                        trigger_price = o.get('triggerPrice', o.get('stopPrice', 'N/A'))
                        has_reduce = o.get('reduceOnly', False)
                        has_close = o.get('closePosition', False)
                        status = "✅ 有保护" if (has_reduce or has_close) else "⚠️ 无保护！"
                        print(f"    algoId={algo_id} | {side} {order_type} "
                              f"| 数量={quantity} | 触发价={trigger_price} "
                              f"| reduceOnly={has_reduce} closePosition={has_close} {status}")
                else:
                    print("  无条件单")
            except Exception as e2:
                print(f"  备用查询也失败: {e2}")

        # 5. 检查近期成交记录，看是否有反手单
        print("\n【5】近期成交记录（最近 20 笔）")
        try:
            trades = await request(session, "GET", "/papi/v1/um/userTrades",
                                   {"symbol": "BTCUSDT", "limit": 20})
            if trades and len(trades) > 0:
                print(f"  共 {len(trades)} 笔成交:")
                for t in reversed(trades):
                    qty = float(t.get('qty', '0'))
                    price = float(t.get('price', '0'))
                    realized_pnl = float(t.get('realizedPnl', '0'))
                    side = t.get('side', 'N/A')
                    print(f"    {t.get('time')} | {side} | 数量={qty} | 价格={price} | PnL={realized_pnl}")
            else:
                print("  无成交记录")
        except Exception as e:
            print(f"  查询成交记录失败: {e}")

        # 6. 检查最近 10 笔条件单成交记录
        print("\n【6】最近条件单成交记录")
        try:
            algo_trades = await request(session, "GET", "/papi/v1/um/algo/history",
                                        {"symbol": "BTCUSDT", "limit": 10})
            if algo_trades and 'data' in algo_trades and len(algo_trades['data']) > 0:
                print(f"  共 {len(algo_trades['data'])} 条记录:")
                for t in algo_trades['data']:
                    algo_id = t.get('algoId', 'N/A')
                    status = t.get('algoStatus', 'N/A')
                    trigger_time = t.get('triggerTime', 'N/A')
                    print(f"    algoId={algo_id} | status={status} | triggerTime={trigger_time}")
            else:
                print("  无条件单历史记录")
        except Exception as e:
            print(f"  查询条件单历史失败: {e}")

    print("\n" + "=" * 70)
    print("  检查完成")
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(main())