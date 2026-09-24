#!/usr/bin/env python3
"""
[只读] 对比当前行情方向与真实持仓方向
验证用户担忧: 行情向上但手上全仓空单是否属实。
1. 用 PM API 查当前所有 USDT 永续持仓（positionAmt）
2. 拉主流币最近 24h / 1h K线判断真实涨跌
不写任何订单, 纯查询。
"""
import asyncio
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from shared.binance_api import BinanceClient

MAIN_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]

async def main():
    client = BinanceClient(
        api_key=os.getenv("BINANCE_API_KEY"),
        api_secret=os.getenv("BINANCE_API_SECRET"),
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
        use_unified_account=os.getenv("BINANCE_USE_PM", "true").lower() == "true"
    )
    await client._init_session()
    try:
        print("=" * 86)
        print("  1) 当前真实持仓 (PM 账户 USDT 永续)")
        print("=" * 86)
        positions = await client.get_position()
        open_pos = [p for p in positions if float(p.get("positionAmt", "0")) != 0]
        if not open_pos:
            print("  （无未平仓持仓）")
        net_long, net_short = 0.0, 0.0
        notional_long, notional_short = 0.0, 0.0
        for p in open_pos:
            amt = float(p.get("positionAmt", "0"))
            price = float(p.get("entryPrice", "0"))
            notional = abs(amt) * price
            side = "多" if amt > 0 else "空"
            if amt > 0:
                long_amt, net_long_max = True, net_long  # 避免未使用告警外的繁琐
                notional_long += notional
            else:
                notional_short += notional
            print(f"  {p.get('symbol',''):<12} {side}  {abs(amt):<12.3f}  入场价 {price:<14.4f}  名义 {notional:,.2f} UDT  "
                  f"市值浮盈PNL: {float(p.get('unrealizedProfit','0')):+.2f} USDT")
        tot = notional_long + notional_short
        if tot > 0:
            print(f"\n  多头名义占比: {notional_long/tot*100:.1f}%   空头名义占比: {notional_short/tot*100:.1f}%")

        print("\n" + "=" * 86)
        print("  2) 主流币近 24h 涨跌 vs 近 1h 涨跌 (USDT 永续)")
        print("=" * 86)
        for sym in MAIN_SYMBOLS:
            try:
                k = await client._request("GET", "/fapi/v1/klines",
                                          {"symbol": sym, "interval": "1h", "limit": 25},
                                          signed=False)
                c1 = float(k[-25][1])  # 25小时前
                c24 = float(k[-25][4])  # 24小时前收盘
                last = float(k[-1][4])
                chg24 = (last - c24) / c24 * 100
                chg1 = (last - c1) / c1 * 100
                bar24 = "↑" if chg24 >= 0 else "↓"
                bar1 = "↑" if chg1 >= 0 else "↓"
                print(f"  {sym:<10} 近24h {chg24:+.2f}% {bar24}   近1h {chg1:+.2f}% {bar1}   现价 {last:,.2f}")
            except Exception as e:
                print(f"  {sym:<10} 查询失败: {e}")

    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())