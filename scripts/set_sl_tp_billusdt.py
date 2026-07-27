#!/usr/bin/env python3
"""
BILLUSDT 止损止盈设置脚本 v2

使用项目 BinanceClient 的 _request 方法直接调用 papi
"""

import os, sys, asyncio
from decimal import Decimal

sys.path.insert(0, "/app")
from shared.binance_api import BinanceClient

SYMBOL = "BILLUSDT"
STOP_LOSS_PCT = Decimal(os.getenv("MANUAL_STOP_LOSS", "0.05"))
TAKE_PROFIT_PCT = Decimal(os.getenv("MANUAL_TAKE_PROFIT", "0.10"))


def fmt_price(price: Decimal, tick_size: Decimal) -> Decimal:
    """格式化为 tick_size 整数倍"""
    return (price / tick_size).to_integral_value() * tick_size


async def main():
    API_KEY = os.environ["BINANCE_API_KEY"]
    API_SECRET = os.environ["BINANCE_API_SECRET"]

    client = BinanceClient(
        api_key=API_KEY, api_secret=API_SECRET,
        testnet=False, use_unified_account=True,
    )

    print("=" * 50)
    print("  BILLUSDT 止损止盈设置")
    print("=" * 50)

    # 1. 查持仓
    print("\n查询持仓...")
    position = await client.get_position(SYMBOL)
    entry = None
    qty = 0
    for p in position:
        if p["symbol"] == SYMBOL:
            amt = float(p["positionAmt"])
            if amt < 0:
                entry = Decimal(p["entryPrice"])
                qty = abs(amt)
                print(f"  做空: {amt} BILLUSDT")
                print(f"  入场价: {entry}")
                break

    if not entry:
        print("未找到做空持仓，退出")
        await client.close()
        return

    # 2. 获取精度
    tick_size = Decimal("0.0000001")  # BILLUSDT 默认7位
    try:
        exchange_info = await client.get_exchange_info()
        for s in exchange_info.get("symbols", []):
            if s.get("symbol") == SYMBOL:
                for f in s.get("filters", []):
                    if f.get("filterType") == "PRICE_FILTER":
                        tick_size = Decimal(f.get("tickSize", "0.0000001"))
                        break
                break
    except Exception:
        pass
    print(f"  tick_size: {tick_size}")

    # 3. 取消已有挂单
    print("\n取消已有挂单...")
    try:
        orders = await client._request("GET", "/papi/v1/um/openOrders", {"symbol": SYMBOL})
        for o in orders:
            oid = o.get("orderId")
            print(f"  取消 {oid} ({o.get('type')})...")
            await client._request("DELETE", "/papi/v1/um/order", {"symbol": SYMBOL, "orderId": oid})
    except Exception as e:
        print(f"  无挂单或取消失败: {e}")

    # 4. 设置止损 (STOP_MARKET + reduceOnly)
    sl_price = fmt_price(entry * (Decimal("1") + STOP_LOSS_PCT), tick_size)
    print(f"\n设置止损 STOP_MARKET: triggerPrice={sl_price}, reduceOnly=true")
    try:
        r = await client._request("POST", "/papi/v1/um/algo/order", {
            "algoType": "CONDITIONAL",
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "STOP_MARKET",
            "triggerPrice": str(sl_price),
            "quantity": str(int(qty)),
            "reduceOnly": "true",
            "workingType": "CONTRACT_PRICE",
        })
        print(f"  成功! algoId={r.get('algoId')}")
    except Exception as e:
        print(f"  失败: {e}")

    # 5. 设置止盈 (TAKE_PROFIT_MARKET + reduceOnly)
    tp_price = fmt_price(entry * (Decimal("1") - TAKE_PROFIT_PCT), tick_size)
    print(f"\n设置止盈 TAKE_PROFIT_MARKET: triggerPrice={tp_price}, reduceOnly=true")
    try:
        r = await client._request("POST", "/papi/v1/um/algo/order", {
            "algoType": "CONDITIONAL",
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": str(tp_price),
            "quantity": str(int(qty)),
            "reduceOnly": "true",
            "workingType": "CONTRACT_PRICE",
        })
        print(f"  成功! algoId={r.get('algoId')}")
    except Exception as e:
        print(f"  失败: {e}")

    print("\n" + "=" * 50)
    print("  完成!")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())