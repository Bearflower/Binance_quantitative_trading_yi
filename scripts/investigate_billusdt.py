#!/usr/bin/env python3
"""
BILLUSDT 仓位变化排查脚本
查询所有订单历史和成交记录，还原仓位变化过程
"""
import os
import sys
import asyncio
import time
import hmac
import hashlib
import json
import logging
from datetime import datetime
from urllib.parse import urlencode

import aiohttp

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("investigate")

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BASE_URL = "https://papi.binance.com"
FAPI_URL = "https://fapi.binance.com"
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
                text = await resp.text()
                if resp.status != 200:
                    logger.error(f"HTTP {resp.status} {method} {endpoint}: {text[:500]}")
                    return None
                return json.loads(text)
        elif method == "POST":
            async with session.post(url, params=params, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error(f"HTTP {resp.status} {method} {endpoint}: {text[:500]}")
                    return None
                return json.loads(text)


def ts_to_str(ts):
    if ts and ts > 0:
        return datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S')
    return "N/A"


async def query_all_orders():
    """查询所有UM订单（包括已成交和已取消）"""
    logger.info("=" * 70)
    logger.info("【1】查询所有 BILLUSDT 订单")
    logger.info("=" * 70)
    all_orders = []
    all_eps = [
        "/papi/v1/um/allOrders",
        "/papi/v1/um/order/all",
    ]
    for ep in all_eps:
        data = await api_request("GET", ep, {"symbol": SYMBOL, "limit": 50})
        if data is not None:
            logger.info(f"端点 {ep} 成功，返回 {len(data)} 条")
            all_orders = data
            break
        logger.warning(f"端点 {ep} 失败")

    if isinstance(all_orders, list) and len(all_orders) > 0:
        for o in all_orders:
            side = o.get('side', '')
            typ = o.get('type', '')
            status = o.get('status', '')
            exec_qty = o.get('executedQty', '0')
            avg_price = o.get('avgPrice', '0')
            cum_quote = o.get('cumQuote', '0')
            update_time = ts_to_str(o.get('updateTime', 0))
            order_id = o.get('orderId', '')
            orig_qty = o.get('origQty', '')
            stop_price = o.get('stopPrice', '')
            trigger_price = o.get('triggerPrice', '')

            logger.info(f"  orderId={order_id} | {side} {typ} | 状态={status}")
            logger.info(f"    数量: {exec_qty}/{orig_qty} | 均价: {avg_price} | 成交额: {cum_quote}")
            logger.info(f"    时间: {update_time}")
            if stop_price:
                logger.info(f"    stopPrice: {stop_price}")
            if trigger_price and trigger_price != '0':
                logger.info(f"    triggerPrice: {trigger_price}")
        return all_orders
    else:
        logger.info("未查询到订单记录")
        return []


async def query_trade_history():
    """查询成交历史"""
    logger.info("=" * 70)
    logger.info("【2】查询 BILLUSDT 成交历史")
    logger.info("=" * 70)
    eps = [
        "/papi/v1/um/userTrades",
        "/papi/v1/um/trades",
    ]
    for ep in eps:
        data = await api_request("GET", ep, {"symbol": SYMBOL, "limit": 50})
        if data is not None:
            logger.info(f"端点 {ep} 成功，返回 {len(data)} 条")
            trades = data
            for t in trades:
                t_id = t.get('id', '')
                price = t.get('price', '')
                qty = t.get('qty', '')
                quote_qty = t.get('quoteQty', '')
                side = t.get('side', '')
                is_buyer = t.get('buyer', False)
                time_str = ts_to_str(t.get('time', 0))
                order_id = t.get('orderId', '')
                realized_pnl = t.get('realizedPnl', '0')
                commission = t.get('commission', '0')

                logger.info(f"  成交id={t_id} | orderId={order_id} | {side} | {'买方' if is_buyer else '卖方'}")
                logger.info(f"    价格: {price} | 数量: {qty} | 成交额: {quote_qty}")
                logger.info(f"    时间: {time_str} | 已实现盈亏: {realized_pnl} | 手续费: {commission} {t.get('commissionAsset','')}")
            return trades
        logger.warning(f"端点 {ep} 失败")
    return []


async def query_income():
    """查询资金流水（用于还原仓位变化）"""
    logger.info("=" * 70)
    logger.info("【3】查询 BILLUSDT 资金流水（最近20条）")
    logger.info("=" * 70)
    data = await api_request("GET", "/papi/v1/um/income", {"symbol": SYMBOL, "limit": 20, "incomeType": "TRANSFER"})
    if data is None:
        data = await api_request("GET", "/papi/v1/um/income", {"symbol": SYMBOL, "limit": 20})
    if isinstance(data, list) and len(data) > 0:
        for item in data:
            income_type = item.get('incomeType', '')
            amount = item.get('income', '')
            time_str = ts_to_str(item.get('time', 0))
            info = item.get('info', '')
            logger.info(f"  [{income_type}] {amount} USDT | {time_str} | {info}")
        return data
    else:
        logger.info("无资金流水记录")
        return []


async def query_algo_orders_via_fapi():
    """尝试通过fapi端点查询algo条件单"""
    logger.info("=" * 70)
    logger.info("【4】尝试多种方式查询algo条件单")
    logger.info("=" * 70)

    # 尝试papi的algo查询
    papi_eps = [
        "/papi/v1/um/algo/orders",
        "/papi/v1/um/openOrder/algo",
        "/papi/v1/um/algoOrder",
    ]
    for ep in papi_eps:
        data = await api_request("GET", ep, {"symbol": SYMBOL})
        if data is not None:
            logger.info(f"papi端点 {ep} 成功: {json.dumps(data, indent=2)[:500]}")
            break
        logger.warning(f"papi端点 {ep} 失败")

    # 尝试fapi端点
    fapi_eps = [
        "/fapi/v1/openOrder",
        "/fapi/v1/allOrders",
    ]
    async with aiohttp.ClientSession() as session:
        for ep in fapi_eps:
            params = {"symbol": SYMBOL}
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = sign(params)
            headers = {"X-MBX-APIKEY": API_KEY}
            url = f"{FAPI_URL}{ep}"
            async with session.get(url, params=params, headers=headers) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        data = json.loads(text)
                        logger.info(f"fapi端点 {ep} 成功: {len(data) if isinstance(data, list) else 'object'}")
                    except:
                        logger.warning(f"fapi端点 {ep} JSON解析失败: {text[:200]}")
                else:
                    logger.warning(f"fapi端点 {ep} HTTP {resp.status}")


async def query_position_history():
    """查询当前持仓详情"""
    logger.info("=" * 70)
    logger.info("【5】查询当前持仓")
    logger.info("=" * 70)
    data = await api_request("GET", "/papi/v1/um/positionRisk")
    if isinstance(data, list):
        for p in data:
            if p.get("symbol") == SYMBOL:
                logger.info(json.dumps(p, indent=2))
                return p
    return None


async def query_account():
    """查询账户概览"""
    logger.info("=" * 70)
    logger.info("【6】查询账户概览")
    logger.info("=" * 70)
    data = await api_request("GET", "/papi/v1/account")
    if data:
        logger.info(f"  账户权益: {data.get('accountEquity')}")
        logger.info(f"  可用余额: {data.get('totalAvailableBalance')}")
        logger.info(f"  总初始保证金: {data.get('totalInitialMargin')}")
        logger.info(f"  总维持保证金: {data.get('totalMaintMargin')}")
        logger.info(f"  UM未实现盈亏: {data.get('totalUmUnrealizedProfit')}")
        return data
    return None


async def main():
    logger.info("=" * 70)
    logger.info("  BILLUSDT 仓位变化排查脚本")
    logger.info(f"  查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    if not API_KEY or not API_SECRET:
        logger.error("API密钥未设置！")
        sys.exit(1)

    # 依次查询
    orders = await query_all_orders()
    trades = await query_trade_history()
    await query_income()
    await query_algo_orders_via_fapi()
    await query_position_history()
    await query_account()

    # 分析结论
    logger.info("=" * 70)
    logger.info("【分析总结】")
    logger.info("=" * 70)

    if isinstance(orders, list) and len(orders) > 0:
        filled_orders = [o for o in orders if o.get('status') == 'FILLED']
        logger.info(f"总订单数: {len(orders)}, 已成交: {len(filled_orders)}")

        # 按时间排序
        sorted_orders = sorted(orders, key=lambda x: x.get('updateTime', 0))

        net_qty = 0
        for o in sorted_orders:
            if o.get('status') == 'FILLED':
                side = o.get('side', '')
                exec_qty = float(o.get('executedQty', 0))
                if side == 'BUY':
                    net_qty += exec_qty
                elif side == 'SELL':
                    net_qty -= exec_qty

        logger.info(f"所有成交净累计数量: {net_qty}")
        logger.info(f"当前持仓: 做多 846 张 @ 0.125298")

        if abs(net_qty - 846) < 1:
            logger.info("✅ 净累计数量和当前持仓一致，说明通过API查询的订单能完整还原仓位")
        else:
            logger.warning(f"⚠️ 净累计数量({net_qty})与当前持仓(846)不一致，可能有未查询到的订单")

    logger.info("排查完成！")


if __name__ == "__main__":
    asyncio.run(main())