#!/usr/bin/env python3
"""
在服务器容器内检查 BTCUSDT 的止盈止损条件单，验证是否缺少 reduceOnly / closePosition 保护
"""
import asyncio
import sys
import os
from decimal import Decimal

# 添加项目根目录到 path
sys.path.insert(0, '/app')

from shared.binance_api import BinanceClient

async def main():
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    api = BinanceClient(api_key=api_key, api_secret=api_secret, use_unified_account=True)
    
    print("=" * 70)
    print("  BTCUSDT 止盈止损条件单检查")
    print("=" * 70)
    
    # 1. 获取当前价格
    print("\n【1】当前行情")
    ticker = await api._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": "BTCUSDT"}, signed=False)
    print(f"  买一价: {ticker.get('bidPrice')}")
    print(f"  卖一价: {ticker.get('askPrice')}")

    # 2. 获取持仓
    print("\n【2】当前持仓")
    positions = await api.get_position("BTCUSDT")
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

    # 3. 获取普通挂单
    print("\n【3】普通挂单")
    try:
        open_orders = await api.get_open_orders("BTCUSDT")
        if open_orders and len(open_orders) > 0:
            print(f"  共 {len(open_orders)} 个挂单:")
            for o in open_orders:
                # 检查是否有保护参数
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

    # 4. 尝试直接调用条件单查询 API
    print("\n【4】条件单查询（直接 API 调用）")
    try:
        # 直接调用 papi 条件单查询接口
        algo_orders = await api._request("GET", "/papi/v1/um/algo/openOrderList", {"symbol": "BTCUSDT"})
        print(f"  响应类型: {type(algo_orders).__name__}")
        if isinstance(algo_orders, list) and len(algo_orders) > 0:
            print(f"  共 {len(algo_orders)} 个条件单:")
            for o in algo_orders:
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
            print(f"  返回结果: {algo_orders}")
    except Exception as e:
        print(f"  查询条件单失败: {e}")

    # 5. 检查近期成交记录
    print("\n【5】近期成交记录（最近 20 笔）")
    try:
        trades = await api._request("GET", "/papi/v1/um/userTrades",
                                    {"symbol": "BTCUSDT", "limit": 20})
        if trades and len(trades) > 0:
            print(f"  共 {len(trades)} 笔成交:")
            for t in reversed(trades):
                qty = float(t.get('qty', '0'))
                price = float(t.get('price', '0'))
                realized_pnl = float(t.get('realizedPnl', '0'))
                side = t.get('side', 'N/A')
                commission = t.get('commission', 'N/A')
                order_id = t.get('orderId', 'N/A')
                print(f"    订单ID={order_id} | {side} | 数量={qty} | 价格={price} | PnL={realized_pnl} | 手续费={commission}")
        else:
            print("  无成交记录")
    except Exception as e:
        print(f"  查询成交记录失败: {e}")

    # 6. 检查最近 10 笔条件单成交记录
    print("\n【6】最近条件单成交记录")
    endpoints_to_try = [
        "/papi/v1/um/algo/history",
        "/papi/v1/algo/history",
    ]
    for endpoint in endpoints_to_try:
        try:
            algo_trades = await api._request("GET", endpoint, {"symbol": "BTCUSDT", "limit": 10})
            if isinstance(algo_trades, dict) and 'data' in algo_trades:
                data = algo_trades['data']
                if data and len(data) > 0:
                    print(f"  [{endpoint}] 共 {len(data)} 条记录:")
                    for t in data:
                        algo_id = t.get('algoId', 'N/A')
                        status = t.get('algoStatus', 'N/A')
                        trigger_time = t.get('triggerTime', 'N/A')
                        print(f"    algoId={algo_id} | status={status} | triggerTime={trigger_time}")
                    break
                else:
                    print(f"  [{endpoint}] 无条件单历史记录")
            else:
                print(f"  [{endpoint}] 返回: {algo_trades}")
        except Exception as e:
            print(f"  [{endpoint}] 失败: {e}")
    
    print("\n" + "=" * 70)
    print("  检查完成")
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(main())