#!/usr/bin/env python3
"""
BILLUSDT 快速查询脚本 - 只查持仓和活跃algo订单
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
logger = logging.getLogger("quick")

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BASE_URL = "https://papi.binance.com"
SYMBOL = "BILLUSDT"


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
                    logger.error(f"HTTP {resp.status}: {text[:500]}")
                    return None
                return json.loads(text)
        elif method == "POST":
            async with session.post(url, params=params, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error(f"HTTP {resp.status}: {text[:500]}")
                    return None
                return json.loads(text)
        elif method == "DELETE":
            async with session.delete(url, params=params, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error(f"HTTP {resp.status}: {text[:500]}")
                    return None
                return json.loads(text)


async def main():
    logger.info("=" * 60)
    logger.info("  BILLUSDT 快速查询")
    logger.info("=" * 60)

    if not API_KEY or not API_SECRET:
        logger.error("API密钥未设置！")
        sys.exit(1)

    # 1. 查询当前价格
    async with aiohttp.ClientSession() as session:
        url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={SYMBOL}"
        async with session.get(url) as resp:
            price_data = await resp.json()
    price = float(price_data.get("price", 0))
    logger.info(f"当前价格: {price}")

    # 2. 查询持仓
    pos_data = await api_request("GET", "/papi/v1/um/positionRisk")
    logger.info(f"--- 持仓查询结果 ---")
    logger.info(f"类型: {type(pos_data).__name__}")
    if isinstance(pos_data, list):
        for p in pos_data:
            if p.get("symbol") == SYMBOL:
                logger.info(f"BILLUSDT 持仓:")
                logger.info(json.dumps(p, indent=2))

    # 3. 查询活跃algo条件单
    algo_data = await api_request("GET", "/papi/v1/um/algo/openOrders")
    logger.info(f"--- 活跃algo条件单 ---")
    logger.info(f"类型: {type(algo_data).__name__}")
    if isinstance(algo_data, list):
        if len(algo_data) == 0:
            logger.info("无活跃algo条件单")
        for o in algo_data:
            if o.get("symbol") == SYMBOL:
                logger.info(f"BILLUSDT algo订单:")
                logger.info(json.dumps(o, indent=2))

    logger.info("查询完成！")


if __name__ == "__main__":
    asyncio.run(main())