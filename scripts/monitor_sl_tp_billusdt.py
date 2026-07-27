#!/usr/bin/env python3
"""
BILLUSDT 软件止损止盈守护进程 v2

背景：统一账户(papi)不支持条件订单，采用 WebSocket 实时监控 + 轮询降级方案

功能：
1. 监听 BILLUSDT miniTicker 实时价格 (wss://fstream.binance.com/ws)
2. 价格触及止损位 → 市价买入平空
3. 价格触及止盈位 → 市价买入平空
4. 每10秒打印状态日志
5. WebSocket 断线自动重连，异常时降级为轮询
"""

import os, sys, asyncio, json, time
from decimal import Decimal
import aiohttp

sys.path.insert(0, "/app")
from shared.binance_api import BinanceClient

# ============================================================
# 配置
# ============================================================
SYMBOL = "BILLUSDT"
STOP_LOSS_PCT = Decimal(os.getenv("MANUAL_STOP_LOSS", "0.05"))     # 止损: +5%
TAKE_PROFIT_PCT = Decimal(os.getenv("MANUAL_TAKE_PROFIT", "0.10"))   # 止盈: -10%
WS_URL = "wss://fstream.binance.com/ws"
CHECK_INTERVAL = 10                  # 价格打印间隔（秒）

# 全局状态
entry_price = Decimal("0")
position_qty = Decimal("0")
stop_loss_price = Decimal("0")
take_profit_price = Decimal("0")
closed = False
last_log_time = 0


async def get_position(client: BinanceClient):
    """获取当前持仓并计算止损止盈价格"""
    global entry_price, position_qty, stop_loss_price, take_profit_price

    print("[初始化] 查询 BILLUSDT 持仓...")
    pos_list = await client.get_position(SYMBOL)

    for p in pos_list:
        if p["symbol"] != SYMBOL:
            continue
        amt = Decimal(p["positionAmt"])
        if amt >= 0:
            print("[初始化] 未找到做空持仓")
            return False
        entry_price = Decimal(p["entryPrice"])
        position_qty = abs(amt)
        stop_loss_price = entry_price * (Decimal("1") + STOP_LOSS_PCT)
        take_profit_price = entry_price * (Decimal("1") - TAKE_PROFIT_PCT)

        print(f"[初始化] 做空 {position_qty} BILLUSDT")
        print(f"[初始化] 入场价: {entry_price}")
        print(f"[初始化] 止损价: {stop_loss_price} (+5%)")
        print(f"[初始化] 止盈价: {take_profit_price} (-10%)")
        return True

    print("[初始化] 未找到 BILLUSDT 持仓")
    return False


async def close_position(client: BinanceClient, reason: str, price: Decimal):
    """市价平仓"""
    global closed
    if closed:
        return
    closed = True

    print(f"\n{'='*50}")
    print(f"  触发条件: {reason}")
    print(f"  当前价格: {price}")
    print(f"  正在市价买入平空 {position_qty} BILLUSDT...")
    print(f"{'='*50}")

    try:
        r = await client._request("POST", "/papi/v1/um/order", {
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "MARKET",
            "quantity": str(position_qty),
            "reduceOnly": "true",
        })
        print(f"[平仓] 成功! orderId={r.get('orderId')}, status={r.get('status')}")
        print(f"[平仓] executedQty={r.get('executedQty')}, avgPrice={r.get('avgPrice')}")
    except Exception as e:
        err_msg = str(e)
        print(f"[平仓] 失败: {err_msg[:120]}")
        print(f"[平仓] 市价平仓失败，请手动处理！切勿不带 reduceOnly 重试！")


async def ws_monitor(client: BinanceClient):
    """WebSocket 价格监控（miniTicker流 + 自动重连）"""
    global last_log_time

    stream = f"{SYMBOL.lower()}@miniTicker"
    ws_url = f"{WS_URL}/{stream}"

    print(f"\n[监控] WebSocket 流: {stream}")
    print(f"[监控] 止损: {stop_loss_price:.7f} | 止盈: {take_profit_price:.7f}")
    print(f"[监控] 开始监控...\n")

    last_log_time = time.time()

    while not closed:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url, heartbeat=30, receive_timeout=60) as ws:
                    print("[监控] WebSocket 已连接")
                    async for msg in ws:
                        if closed:
                            break

                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            mark_price = Decimal(str(data.get("c", 0)))

                            if mark_price <= 0:
                                continue

                            # 检查止损止盈
                            if mark_price >= stop_loss_price:
                                await close_position(client, 
                                    f"止损触发 ({mark_price:.7f} >= {stop_loss_price:.7f})", mark_price)
                            elif mark_price <= take_profit_price:
                                await close_position(client, 
                                    f"止盈触发 ({mark_price:.7f} <= {take_profit_price:.7f})", mark_price)

                            # 定期打印状态
                            now = time.time()
                            if now - last_log_time >= CHECK_INTERVAL:
                                pnl = (entry_price - mark_price) * position_qty / entry_price * Decimal("2")
                                status = "[已平仓]" if closed else "[监控中]"
                                print(
                                    f"{status} price={mark_price:.7f} "
                                    f"SL={stop_loss_price:.7f} "
                                    f"TP={take_profit_price:.7f} "
                                    f"PnL={pnl:.4f}USDT"
                                )
                                last_log_time = now

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            print(f"[监控] 连接关闭 ({msg.type}), 5秒后重连...")
                            break

        except asyncio.TimeoutError:
            print("[监控] 超时, 5秒后重连...")
        except aiohttp.ClientConnectorError as e:
            print(f"[监控] 连接失败: {e}, 10秒后重连...")
            await asyncio.sleep(10)
        except Exception as e:
            print(f"[监控] 异常: {e}, 5秒后重连...")
        
        if not closed:
            await asyncio.sleep(5)

    print("[监控] 监控循环退出")


async def polling_monitor(client: BinanceClient):
    """降级轮询监控方案"""
    global last_log_time
    fapi_url = "https://fapi.binance.com"
    last_log_time = 0

    print(f"\n[监控] 降级为轮询模式 (间隔5秒)")
    
    async with aiohttp.ClientSession() as session:
        while not closed:
            try:
                async with session.get(
                    f"{fapi_url}/fapi/v1/ticker/price",
                    params={"symbol": SYMBOL},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    if data and "price" in data:
                        mark_price = Decimal(data["price"])

                        if not closed:
                            if mark_price >= stop_loss_price:
                                await close_position(client, f"止损触发", mark_price)
                            elif mark_price <= take_profit_price:
                                await close_position(client, f"止盈触发", mark_price)

                        now = time.time()
                        if now - last_log_time >= CHECK_INTERVAL:
                            pnl = (entry_price - mark_price) * position_qty / entry_price * Decimal("2")
                            print(f"[轮询中] price={mark_price:.7f} PnL={pnl:.4f}USDT")
                            last_log_time = now
            except Exception as e:
                print(f"[轮询] 请求失败: {e}")
            
            await asyncio.sleep(5)


async def main():
    API_KEY = os.environ["BINANCE_API_KEY"]
    API_SECRET = os.environ["BINANCE_API_SECRET"]

    client = BinanceClient(
        api_key=API_KEY, api_secret=API_SECRET,
        testnet=False, use_unified_account=True,
    )

    print("=" * 50)
    print("  BILLUSDT 软件止损止盈守护进程 v2")
    print("=" * 50)

    # 1. 获取持仓
    has_pos = await get_position(client)
    if not has_pos:
        await client.close()
        return

    # 2. 直接使用轮询模式（国内服务器连币安 WebSocket 可能被屏蔽）
    print("[监控] 使用轮询模式 (国内服务器 WebSocket 不稳定)\n")
    await polling_monitor(client)

    await client.close()
    print("\n[结束] 守护进程退出")


if __name__ == "__main__":
    asyncio.run(main())