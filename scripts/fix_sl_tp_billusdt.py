#!/usr/bin/env python3
"""
BILLUSDT 止盈止损条件单修复脚本
- 取消现有的错误algo条件单（如果有）
- 为当前做多仓位设置正确的止盈止损
"""
import os
import sys
import asyncio
import time
import hmac
import hashlib
import json
import logging
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import aiohttp

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("fix_sl_tp")

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BASE_URL = "https://papi.binance.com"
SYMBOL = "BILLUSDT"

STOP_LOSS_PCT = 0.05   # 止损：入场价 -5%
TAKE_PROFIT_PCT = 0.10 # 止盈：入场价 +10%


def sign(params):
    query_string = urlencode(params)
    return hmac.new(API_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()


async def api_request(method, endpoint, params=None):
    if params is None:
        params = {}
    params.pop('signature', None)
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = sign(params)
    headers = {"X-MBX-APIKEY": API_KEY}
    url = f"{BASE_URL}{endpoint}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if method == "GET":
            async with session.get(url, params=params, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error(f"HTTP {resp.status} {method} {endpoint}: {text[:300]}")
                    return None
                return json.loads(text)
        elif method == "POST":
            async with session.post(url, params=params, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error(f"HTTP {resp.status} {method} {endpoint}: {text[:300]}")
                    return None
                return json.loads(text)
        elif method == "DELETE":
            async with session.delete(url, params=params, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error(f"HTTP {resp.status} {method} {endpoint}: {text[:300]}")
                    return None
                return json.loads(text)


def format_price(p, precision=4):
    d = Decimal(str(p))
    fmt = Decimal('1.' + '0' * precision)
    return float(d.quantize(fmt))


async def get_price_precision():
    """获取价格精度"""
    async with aiohttp.ClientSession() as session:
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        async with session.get(url) as resp:
            data = await resp.json()
    for s in data.get('symbols', []):
        if s['symbol'] == SYMBOL:
            pp = s.get('pricePrecision', 4)
            logger.info(f"{SYMBOL} 价格精度: {pp}位")
            return pp
    return 4


async def get_position():
    """获取当前持仓"""
    data = await api_request("GET", "/papi/v1/um/positionRisk")
    if isinstance(data, list):
        for p in data:
            if p.get("symbol") == SYMBOL:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    return p
    return None


async def query_open_algo_orders():
    """尝试查询活跃的algo条件单"""
    logger.info("尝试查询活跃algo条件单...")
    eps = [
        "/papi/v1/um/algo/openOrders",
        "/papi/v1/algo/openOrders",
    ]
    for ep in eps:
        data = await api_request("GET", ep, {"symbol": SYMBOL})
        if data is not None:
            logger.info(f"端点 {ep} 成功，返回 {len(data) if isinstance(data, list) else 'N/A'} 条")
            return data if isinstance(data, list) else []
        logger.warning(f"端点 {ep} 失败")
    return []


async def cancel_algo_order(algo_id):
    """取消algo条件单"""
    logger.info(f"取消algo条件单 algoId={algo_id}...")
    data = await api_request("DELETE", "/papi/v1/um/algo/order", {"algoId": algo_id, "symbol": SYMBOL})
    if data:
        logger.info(f"取消成功: {json.dumps(data, indent=2)}")
        return True
    return False


async def create_algo_order(algo_type, side, quantity, trigger_price, reduce_only=True):
    """创建algo条件单"""
    params = {
        "algoType": "CONDITIONAL",
        "symbol": SYMBOL,
        "side": side,
        "type": algo_type,
        "quantity": str(quantity),
        "triggerPrice": str(trigger_price),
        "reduceOnly": "true" if reduce_only else "false",
        "workingType": "CONTRACT_PRICE",
    }
    logger.info(f"创建algo条件单: type={algo_type}, side={side}, quantity={quantity}, triggerPrice={trigger_price}")
    data = await api_request("POST", "/papi/v1/um/algo/order", params)
    if data:
        logger.info(f"创建成功:")
        logger.info(json.dumps(data, indent=2))
        return data
    return None


async def main():
    logger.info("=" * 60)
    logger.info("  BILLUSDT 止盈止损修复脚本")
    logger.info(f"  止损: -{STOP_LOSS_PCT*100:.0f}%")
    logger.info(f"  止盈: +{TAKE_PROFIT_PCT*100:.0f}%")
    logger.info("=" * 60)

    if not API_KEY or not API_SECRET:
        logger.error("API密钥未设置！")
        sys.exit(1)

    # 1. 获取价格精度
    price_precision = await get_price_precision()

    # 2. 获取当前持仓
    position = await get_position()
    if position is None:
        logger.error(f"未找到 {SYMBOL} 持仓！")
        sys.exit(1)

    amt = float(position.get("positionAmt", 0))
    entry = float(position.get("entryPrice", 0))
    direction = "做多" if amt > 0 else "做空"

    logger.info(f"持仓: {direction} {abs(amt)} 张 @ {entry}")

    # 3. 查询并取消现有的algo条件单
    algo_orders = await query_open_algo_orders()
    if algo_orders and len(algo_orders) > 0:
        logger.info(f"发现 {len(algo_orders)} 个活跃algo条件单，开始取消...")
        for o in algo_orders:
            await cancel_algo_order(o.get("algoId"))
    else:
        logger.info("无活跃algo条件单，无需取消")

    # 4. 计算止盈止损价格
    if amt > 0:
        # 做多仓位
        stop_price = format_price(entry * (1 - STOP_LOSS_PCT), price_precision)
        tp_price = format_price(entry * (1 + TAKE_PROFIT_PCT), price_precision)
        close_side = "SELL"
    else:
        # 做空仓位
        stop_price = format_price(entry * (1 + STOP_LOSS_PCT), price_precision)
        tp_price = format_price(entry * (1 - TAKE_PROFIT_PCT), price_precision)
        close_side = "BUY"

    quantity = abs(amt)

    logger.info(f"止损触发价: {stop_price} (入场价 {direction} {STOP_LOSS_PCT*100:.0f}%)")
    logger.info(f"止盈触发价: {tp_price} (入场价 {'+' if amt > 0 else '-'}{TAKE_PROFIT_PCT*100:.0f}%)")
    logger.info(f"平仓方向: {close_side}, 数量: {quantity}")

    # 5. 设置止损
    logger.info("-" * 40)
    sl_result = await create_algo_order("STOP_MARKET", close_side, quantity, stop_price)
    if sl_result:
        logger.info(f"止损单已创建: algoId={sl_result.get('algoId')}")
    else:
        logger.error("止损单创建失败！")

    # 6. 设置止盈
    logger.info("-" * 40)
    tp_result = await create_algo_order("TAKE_PROFIT_MARKET", close_side, quantity, tp_price)
    if tp_result:
        logger.info(f"止盈单已创建: algoId={tp_result.get('algoId')}")
    else:
        logger.error("止盈单创建失败！")

    # 7. 打印摘要
    logger.info("=" * 60)
    logger.info("  操作完成！")
    logger.info(f"  持仓: {direction} {quantity} 张 @ {entry}")
    logger.info(f"  止损单: STOP_MARKET, {close_side}, triggerPrice={stop_price}")
    logger.info(f"  止盈单: TAKE_PROFIT_MARKET, {close_side}, triggerPrice={tp_price}")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())