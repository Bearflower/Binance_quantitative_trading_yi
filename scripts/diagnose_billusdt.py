#!/usr/bin/env python3
"""
BILLUSDT 仓位诊断脚本
查询当前持仓、algo条件单、订单历史、账户余额
"""
import os
import sys
import asyncio
import time
import hmac
import hashlib
import json
import logging
from urllib.parse import urlencode

import aiohttp

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("diagnose")

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BASE_URL = "https://papi.binance.com"
SYMBOL = "BILLUSDT"


def sign(params):
    query_string = urlencode(params)
    return hmac.new(API_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()


async def api_request(method, endpoint, params=None, base_url=BASE_URL):
    if params is None:
        params = {}
    params.pop('signature', None)
    params['timestamp'] = int(time.time() * 1000)
    params['signature'] = sign(params)
    headers = {"X-MBX-APIKEY": API_KEY}
    url = f"{base_url}{endpoint}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if method == "GET":
            async with session.get(url, params=params, headers=headers) as resp:
                return await resp.json()
        elif method == "POST":
            async with session.post(url, params=params, headers=headers) as resp:
                return await resp.json()
        elif method == "DELETE":
            async with session.delete(url, params=params, headers=headers) as resp:
                return await resp.json()


async def get_positions():
    """查询UM持仓"""
    logger.info("=" * 60)
    logger.info("【1】查询 BILLUSDT 持仓")
    logger.info("=" * 60)
    data = await api_request("GET", "/papi/v1/um/positionRisk")
    if isinstance(data, list):
        for p in data:
            if p.get("symbol") == SYMBOL:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    logger.info(f"持仓发现！")
                    logger.info(f"  交易对: {p.get('symbol')}")
                    logger.info(f"  数量: {amt}")
                    logger.info(f"  方向: {'做多' if amt > 0 else '做空'}")
                    logger.info(f"  入场价: {p.get('entryPrice')}")
                    logger.info(f"  标记价: {p.get('markPrice')}")
                    logger.info(f"  未实现盈亏: {p.get('unRealizedProfit')} USDT")
                    logger.info(f"  杠杆: {p.get('leverage')}x")
                    logger.info(f"  清算价: {p.get('liquidationPrice')}")
                    return p
                else:
                    logger.info(f"BILLUSDT 持仓量为0，无持仓")
                    return None
    logger.info("BILLUSDT 未在持仓列表中找到")
    return None


async def get_algo_orders():
    """查询活动中的algo条件单"""
    logger.info("=" * 60)
    logger.info("【2】查询活动中 BILLUSDT algo条件单")
    logger.info("=" * 60)
    data = await api_request("GET", "/papi/v1/um/algo/openOrders", {"symbol": SYMBOL})
    if isinstance(data, list):
        if len(data) == 0:
            logger.info("无活动中的algo条件单")
        for o in data:
            logger.info(f"  algoId: {o.get('algoId')}")
            logger.info(f"  clientAlgoId: {o.get('clientAlgoId')}")
            logger.info(f"  algoType: {o.get('algoType')}")
            logger.info(f"  orderType: {o.get('orderType')} (type)")
            logger.info(f"  side: {o.get('side')}")
            logger.info(f"  positionSide: {o.get('positionSide')}")
            logger.info(f"  triggerPrice: {o.get('triggerPrice')}")
            logger.info(f"  price: {o.get('price')}")
            logger.info(f"  quantity: {o.get('quantity')}")
            logger.info(f"  reduceOnly: {o.get('reduceOnly')}")
            logger.info(f"  algoStatus: {o.get('algoStatus')}")
            logger.info(f"  workingType: {o.get('workingType')}")
            logger.info(f"  priceProtect: {o.get('priceProtect')}")
            logger.info(f"  createTime: {o.get('createTime')}")
            logger.info("  ---")
        return data
    return []


async def get_algo_history():
    """查询algo条件单历史"""
    logger.info("=" * 60)
    logger.info("【3】查询 BILLUSDT algo条件单历史（最近10条）")
    logger.info("=" * 60)
    data = await api_request("GET", "/papi/v1/um/algo/history", {"symbol": SYMBOL, "limit": 10})
    if isinstance(data, list):
        if len(data) == 0:
            logger.info("无algo条件单历史记录")
        for o in data:
            logger.info(f"  algoId: {o.get('algoId')}")
            logger.info(f"  orderType: {o.get('orderType')}")
            logger.info(f"  side: {o.get('side')}")
            logger.info(f"  triggerPrice: {o.get('triggerPrice')}")
            logger.info(f"  quantity: {o.get('quantity')}")
            logger.info(f"  algoStatus: {o.get('algoStatus')}")
            logger.info(f"  updateTime: {o.get('updateTime')}")
            logger.info("  ---")
        return data
    return []


async def get_order_history():
    """查询最近订单历史"""
    logger.info("=" * 60)
    logger.info("【4】查询 BILLUSDT 最近订单（最近5条）")
    logger.info("=" * 60)
    data = await api_request("GET", "/papi/v1/um/order", {"symbol": SYMBOL, "limit": 5})
    if isinstance(data, list):
        for o in data:
            logger.info(f"  orderId: {o.get('orderId')}")
            logger.info(f"  side: {o.get('side')}")
            logger.info(f"  type: {o.get('type')}")
            logger.info(f"  status: {o.get('status')}")
            logger.info(f"  executedQty: {o.get('executedQty')}")
            logger.info(f"  avgPrice: {o.get('avgPrice')}")
            logger.info(f"  totalQuoteQty: {o.get('cumQuote')}")
            logger.info(f"  updateTime: {o.get('updateTime')}")
            logger.info("  ---")
        return data
    return []


async def get_account():
    """查询账户信息"""
    logger.info("=" * 60)
    logger.info("【5】查询账户信息")
    logger.info("=" * 60)
    data = await api_request("GET", "/papi/v1/account")
    if data:
        logger.info(f"  账户权益(accountEquity): {data.get('accountEquity')}")
        logger.info(f"  可用余额(totalAvailableBalance): {data.get('totalAvailableBalance')}")
        logger.info(f"  总初始保证金(totalInitialMargin): {data.get('totalInitialMargin')}")
        logger.info(f"  总维持保证金(totalMaintMargin): {data.get('totalMaintMargin')}")
        return data
    return {}


async def get_current_price():
    """获取当前价格"""
    async with aiohttp.ClientSession() as session:
        url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={SYMBOL}"
        async with session.get(url) as resp:
            data = await resp.json()
            price = float(data.get("price", 0))
            logger.info(f"【6】{SYMBOL} 当前价格: {price}")
            return price


async def main():
    logger.info("=" * 60)
    logger.info("  BILLUSDT 仓位诊断脚本")
    logger.info("=" * 60)

    if not API_KEY or not API_SECRET:
        logger.error("API密钥未设置！请设置环境变量 BINANCE_API_KEY 和 BINANCE_API_SECRET")
        sys.exit(1)

    # 并行查询
    results = await asyncio.gather(
        get_positions(),
        get_algo_orders(),
        get_algo_history(),
        get_order_history(),
        get_account(),
        get_current_price(),
    )

    position = results[0]
    algo_orders = results[1]
    price = results[5]

    # 综合诊断
    logger.info("=" * 60)
    logger.info("【综合诊断结论】")
    logger.info("=" * 60)

    if position:
        amt = float(position.get("positionAmt", 0))
        entry = float(position.get("entryPrice", 0))
        direction = "做多" if amt > 0 else "做空"
        logger.info(f"  持仓方向: {direction}")
        logger.info(f"  持仓数量: {abs(amt)} 张")
        logger.info(f"  入场均价: {entry}")
        if price > 0:
            if amt < 0:
                pnl_pct = (entry - price) / entry * 100
                logger.info(f"  价格变动: {price} (入场 {entry}), 做空盈亏: {pnl_pct:+.2f}%")
            else:
                pnl_pct = (price - entry) / entry * 100
                logger.info(f"  价格变动: {price} (入场 {entry}), 做多盈亏: {pnl_pct:+.2f}%")
    else:
        logger.info("  当前无BILLUSDT持仓")

    if isinstance(algo_orders, list) and len(algo_orders) > 0:
        logger.info(f"  活跃algo条件单: {len(algo_orders)} 个")
        for o in algo_orders:
            logger.info(f"    algoId={o.get('algoId')}, type={o.get('orderType')}, side={o.get('side')}, triggerPrice={o.get('triggerPrice')}")
    else:
        logger.info("  活跃algo条件单: 0 个")

    logger.info("诊断完成！")


if __name__ == "__main__":
    asyncio.run(main())