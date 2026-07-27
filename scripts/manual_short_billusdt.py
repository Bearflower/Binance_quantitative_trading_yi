#!/usr/bin/env python3
"""
BILLUSDT 手动开空仓脚本（独立运行版 - 统一账户 PM）

功能：
1. 连接币安统一账户 API（papi.binance.com）
2. 查询 BILLUSDT 当前价格和合约规格
3. 使用 50% 本金（约 25 USDT）作为保证金，2x 杠杆开空仓
4. 自动设置止损（+5%）和止盈（-10%）

用法：
    python manual_short_billusdt.py              # 交互模式（需手动确认）
    python manual_short_billusdt.py --yes        # 自动确认模式（跳过交互）

环境变量：
    BINANCE_API_KEY    - 币安 API 密钥
    BINANCE_API_SECRET - 币安 API 密钥
    MANUAL_MARGIN      - 保证金（默认 25 USDT）
    MANUAL_STOP_LOSS   - 止损百分比（默认 0.05）
    MANUAL_TAKE_PROFIT - 止盈百分比（默认 0.10）
"""

import os
import sys
import argparse
import time
import hmac
import hashlib
import asyncio
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any
from urllib.parse import urlencode

import aiohttp


# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("manual_short")


# ============================================================
# 配置参数
# ============================================================
SYMBOL = "BILLUSDT"
LEVERAGE = int(os.getenv("MANUAL_LEVERAGE", "2"))
MARGIN_USDT = float(os.getenv("MANUAL_MARGIN", "25"))
STOP_LOSS_PERCENT = float(os.getenv("MANUAL_STOP_LOSS", "0.05"))
TAKE_PROFIT_PERCENT = float(os.getenv("MANUAL_TAKE_PROFIT", "0.10"))

# API 配置 - 统一账户使用 papi 端点
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BASE_URL = "https://papi.binance.com"   # 统一账户端点
FAPI_URL = "https://fapi.binance.com"   # 公开数据端点


def generate_signature(params: Dict[str, Any]) -> str:
    """生成 HMAC-SHA256 签名"""
    query_string = urlencode(params)
    return hmac.new(
        API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


async def papi_request(
    method: str,
    endpoint: str,
    params: Optional[Dict] = None,
    signed: bool = True
) -> Any:
    """
    统一账户 API 请求
    
    签名请求走 BASE_URL (papi)，公开数据走 FAPI_URL (fapi)
    """
    if params is None:
        params = {}
    
    if signed and API_KEY:
        params.pop('signature', None)
        params['timestamp'] = int(time.time() * 1000)
        params['signature'] = generate_signature(params)
        url = f"{BASE_URL}{endpoint}"
    else:
        url = f"{FAPI_URL}{endpoint}"
    
    headers = {"X-MBX-APIKEY": API_KEY} if signed else {}
    
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if method.upper() == "GET":
            async with session.get(url, params=params, headers=headers) as resp:
                data = await resp.json()
        elif method.upper() == "POST":
            async with session.post(url, params=params, headers=headers) as resp:
                data = await resp.json()
        else:
            raise ValueError(f"不支持的HTTP方法: {method}")
        
        if resp.status != 200:
            code = data.get('code', resp.status)
            msg = data.get('msg', str(resp.status))
            logger.error(f"API错误 [{code}]: {msg}")
            return None
        
        return data


async def get_account_balance() -> float:
    """获取账户 USDT 可用余额"""
    logger.info("正在查询账户余额...")
    try:
        # 统一账户使用 /papi/v1/account 获取账户信息
        account = await papi_request("GET", "/papi/v1/account")
        if account is None:
            logger.error("获取账户信息失败")
            return 0.0
        
        total_available = float(account.get('totalAvailableBalance', 0))
        account_equity = float(account.get('accountEquity', 0))
        logger.info(
            f"账户权益: {account_equity:.2f} USDT, "
            f"可用余额: {total_available:.2f} USDT"
        )
        return total_available
        
    except Exception as e:
        logger.error(f"获取账户余额失败: {e}")
        return 0.0


async def get_current_price() -> float:
    """获取 BILLUSDT 当前价格（公开数据，走 fapi）"""
    logger.info(f"正在查询 {SYMBOL} 当前价格...")
    data = await papi_request("GET", "/fapi/v1/ticker/price", {"symbol": SYMBOL}, signed=False)
    if data is None:
        logger.error("获取价格失败")
        return 0.0
    price = float(data.get('price', 0))
    logger.info(f"{SYMBOL} 当前价格: {price}")
    return price


async def get_symbol_info():
    """获取交易对精度信息（公开数据，走 fapi）"""
    logger.info(f"正在查询 {SYMBOL} 合约规格...")
    data = await papi_request("GET", "/fapi/v1/exchangeInfo", signed=False)
    if data is None:
        logger.error("获取交易所信息失败")
        return 2, 0, 0.001
    
    for s in data.get('symbols', []):
        if s['symbol'] != SYMBOL:
            continue
        
        price_precision = s.get('pricePrecision', 2)
        quantity_precision = s.get('quantityPrecision', 0)
        
        min_qty = 0.0
        step_size = 0.001
        min_notional = 0.0
        
        for f in s.get('filters', []):
            if f['filterType'] == 'LOT_SIZE':
                min_qty = float(f.get('minQty', 0))
                step_size = float(f.get('stepSize', 0.001))
            elif f['filterType'] == 'MIN_NOTIONAL':
                min_notional = float(f.get('notional', 0))
        
        logger.info(
            f"{SYMBOL} 合约规格: "
            f"价格精度={price_precision}位, "
            f"数量精度={quantity_precision}位, "
            f"最小数量={min_qty}, "
            f"步长={step_size}, "
            f"最小名义价值={min_notional} USDT"
        )
        return price_precision, quantity_precision, step_size, min_qty
    
    logger.error(f"未找到 {SYMBOL} 交易对信息！")
    return 2, 0, 0.001, 0.0


def format_quantity(quantity: float, step_size: float, quantity_precision: int) -> float:
    """按精度格式化数量（向下取整）"""
    q = Decimal(str(quantity))
    s = Decimal(str(step_size))
    result = (q / s).to_integral_value(rounding=ROUND_DOWN) * s
    formatted = float(result.quantize(Decimal('1.' + '0' * quantity_precision)))
    return formatted


def format_price(price: float, price_precision: int) -> float:
    """按精度格式化价格"""
    p = Decimal(str(price))
    return float(p.quantize(Decimal('1.' + '0' * price_precision)))


def calculate_quantity(
    margin_usdt: float,
    current_price: float,
    leverage: int,
    step_size: float,
    quantity_precision: int,
    min_qty: float
) -> float:
    """计算开仓数量"""
    position_value = margin_usdt * leverage
    raw_quantity = position_value / current_price
    quantity = format_quantity(raw_quantity, step_size, quantity_precision)
    
    logger.info(
        f"仓位计算: 保证金={margin_usdt} USDT, "
        f"杠杆={leverage}x, "
        f"开仓价值={position_value:.2f} USDT, "
        f"原始数量={raw_quantity:.6f}, "
        f"格式化数量={quantity}"
    )
    
    if quantity < min_qty:
        logger.error(f"计算出的数量 {quantity} 小于最小数量限制 {min_qty}！")
        return 0.0
    return quantity


async def set_leverage(leverage: int):
    """设置杠杆倍数"""
    logger.info(f"正在设置 {SYMBOL} 杠杆为 {leverage}x...")
    result = await papi_request(
        "POST", "/papi/v1/um/leverage",
        {"symbol": SYMBOL, "leverage": leverage}
    )
    if result:
        logger.info(f"杠杆设置成功: {SYMBOL} x{result.get('leverage')}")
    else:
        logger.error("设置杠杆失败")


async def place_short_order(quantity: float) -> Optional[Dict]:
    """开空仓（市价单）"""
    logger.info(f"准备开空仓: {SYMBOL}, 数量={quantity}")
    
    result = await papi_request(
        "POST", "/papi/v1/um/order",
        {
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "MARKET",
            "quantity": str(quantity),
        }
    )
    
    if result:
        logger.info(
            f"开空仓成功！订单ID: {result.get('orderId')}, "
            f"状态: {result.get('status')}, "
            f"成交数量: {result.get('executedQty')}, "
            f"成交均价: {result.get('avgPrice')}"
        )
    else:
        logger.error("开空仓失败！")
    
    return result


async def set_stop_loss(entry_price: float, quantity: float, price_precision: int) -> Optional[Dict]:
    """
    设置止损单
    
    做空止损逻辑：
    - 价格上涨 5% 意味着亏损，触发止损买入平仓
    - stop_price = entry_price * (1 + 5%)
    
    papi 端点需使用 quantity + reduceOnly 而非 closePosition
    """
    stop_price_raw = entry_price * (1 + STOP_LOSS_PERCENT)
    stop_price = format_price(stop_price_raw, price_precision)
    
    logger.info(
        f"设置止损: 入场价={entry_price}, "
        f"止损价={stop_price} (入场价 +{STOP_LOSS_PERCENT*100:.1f}%), "
        f"数量={quantity}"
    )
    
    result = await papi_request(
        "POST", "/papi/v1/um/algo/order",
        {
            "algoType": "CONDITIONAL",
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "STOP_MARKET",
            "triggerPrice": str(stop_price),
            "quantity": str(quantity),
            "reduceOnly": "true",
            "workingType": "CONTRACT_PRICE",
        }
    )
    
    if result:
        logger.info(f"止损单设置成功！订单ID: {result.get('orderId')}")
    else:
        logger.error("设置止损失败！")
    
    return result


async def set_take_profit(entry_price: float, quantity: float, price_precision: int) -> Optional[Dict]:
    """
    设置止盈单
    
    做空止盈逻辑：
    - 价格下跌 10% 意味着盈利，触发止盈买入平仓
    - tp_price = entry_price * (1 - 10%)
    
    papi 端点需使用 quantity + reduceOnly 而非 closePosition
    """
    tp_price_raw = entry_price * (1 - TAKE_PROFIT_PERCENT)
    tp_price = format_price(tp_price_raw, price_precision)
    
    logger.info(
        f"设置止盈: 入场价={entry_price}, "
        f"止盈价={tp_price} (入场价 -{TAKE_PROFIT_PERCENT*100:.1f}%), "
        f"数量={quantity}"
    )
    
    result = await papi_request(
        "POST", "/papi/v1/um/algo/order",
        {
            "algoType": "CONDITIONAL",
            "symbol": SYMBOL,
            "side": "BUY",
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": str(tp_price),
            "quantity": str(quantity),
            "reduceOnly": "true",
            "workingType": "CONTRACT_PRICE",
        }
    )
    
    if result:
        logger.info(f"止盈单设置成功！订单ID: {result.get('orderId')}")
    else:
        logger.error("设置止盈失败！")
    
    return result


def print_confirmation(
    current_price: float,
    quantity: float,
    entry_price: float,
    stop_loss_price: float,
    take_profit_price: float,
    margin: float
):
    """打印交易确认信息"""
    print()
    print("=" * 60)
    print("  BILLUSDT 手动做空 - 交易确认")
    print("=" * 60)
    print(f"  交易对:       {SYMBOL}")
    print(f"  方向:         做空 (SELL)")
    print(f"  当前价格:     {current_price}")
    print(f"  入场价格:     {entry_price}")
    print(f"  开仓数量:     {quantity}")
    print(f"  杠杆倍数:     {LEVERAGE}x")
    print(f"  保证金:       {margin:.2f} USDT")
    print(f"  开仓价值:     {margin * LEVERAGE:.2f} USDT")
    print(f"  止损价格:     {stop_loss_price} (入场价 +{STOP_LOSS_PERCENT*100:.1f}%)")
    print(f"  止盈价格:     {take_profit_price} (入场价 -{TAKE_PROFIT_PERCENT*100:.1f}%)")
    print("=" * 60)
    print()


def confirm_execution() -> bool:
    """等待用户确认"""
    print()
    try:
        response = input("确认执行以上做空交易？[y/N]: ").strip().lower()
        return response in ('y', 'yes')
    except (KeyboardInterrupt, EOFError):
        print("\n已取消操作")
        return False


async def run(auto_confirm: bool = False):
    """主异步函数"""
    parser = argparse.ArgumentParser(description="BILLUSDT 手动做空脚本")
    parser.add_argument("-y", "--yes", action="store_true", dest="auto_confirm", default=False, help="自动确认")
    parser.add_argument("--fix-sl-tp", action="store_true", default=False, help="仅修复止损止盈（不新开仓）")
    parser.add_argument("--entry", type=float, default=0, help="入场价格（修复模式使用）")
    parser.add_argument("--qty", type=float, default=0, help="持仓数量（修复模式使用）")
    logger.info("=" * 60)
    logger.info("  BILLUSDT 手动做空脚本启动 (统一账户-PAPI)")
    logger.info(f"  交易对: {SYMBOL}")
    logger.info(f"  杠杆: {LEVERAGE}x")
    logger.info(f"  保证金: {MARGIN_USDT} USDT")
    logger.info(f"  止损: +{STOP_LOSS_PERCENT*100:.1f}%")
    logger.info(f"  止盈: -{TAKE_PROFIT_PERCENT*100:.1f}%")
    logger.info(f"  确认模式: {'自动确认' if auto_confirm else '交互确认'}")
    logger.info(f"  API端点: {BASE_URL}")
    logger.info("=" * 60)
    
    if not API_KEY or not API_SECRET:
        logger.error("API密钥未设置！请检查环境变量 BINANCE_API_KEY 和 BINANCE_API_SECRET")
        sys.exit(1)
    
    # 1. 检查账户余额
    balance = await get_account_balance()
    if balance <= 0:
        logger.error("账户余额不足，无法交易！")
        sys.exit(1)
    
    if balance < MARGIN_USDT:
        logger.warning(
            f"账户可用余额 ({balance:.2f} USDT) 小于计划保证金 ({MARGIN_USDT} USDT)，"
            f"将使用全部可用余额"
        )
        actual_margin = balance
    else:
        actual_margin = MARGIN_USDT
    
    # 2. 获取当前价格
    current_price = await get_current_price()
    if current_price <= 0:
        logger.error("无法获取当前价格！")
        sys.exit(1)
    
    # 3. 获取合约规格
    price_precision, quantity_precision, step_size, min_qty = await get_symbol_info()
    
    # 4. 计算开仓数量
    quantity = calculate_quantity(
        margin_usdt=actual_margin,
        current_price=current_price,
        leverage=LEVERAGE,
        step_size=step_size,
        quantity_precision=quantity_precision,
        min_qty=min_qty
    )
    
    if quantity <= 0:
        logger.error("计算出的数量无效，无法开仓！")
        sys.exit(1)
    
    position_value = quantity * current_price
    logger.info(f"预计开仓名义价值: {position_value:.2f} USDT")
    
    # 5. 计算止盈止损价格
    entry_price = format_price(current_price, price_precision)
    stop_loss_price = format_price(entry_price * (1 + STOP_LOSS_PERCENT), price_precision)
    take_profit_price = format_price(entry_price * (1 - TAKE_PROFIT_PERCENT), price_precision)
    
    # 6. 打印确认信息
    print_confirmation(
        current_price=current_price,
        quantity=quantity,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        margin=actual_margin
    )
    
    # 7. 确认执行
    if auto_confirm:
        logger.info("自动确认模式，跳过交互确认")
    elif not confirm_execution():
        logger.info("用户取消操作，脚本退出")
        sys.exit(0)
    
    logger.info("用户已确认，开始执行交易...")
    logger.info("-" * 40)
    
    # 8. 设置杠杆
    await set_leverage(LEVERAGE)
    
    # 9. 开空仓
    order = await place_short_order(quantity)
    if not order:
        logger.error("开空仓失败，脚本终止！")
        sys.exit(1)
    
    # 10. 获取实际成交均价
    avg_price_str = order.get('avgPrice')
    if avg_price_str and float(avg_price_str) > 0:
        filled_price = float(avg_price_str)
    else:
        filled_price = current_price
    
    logger.info(f"实际成交均价: {filled_price}")
    
    # 重新计算基于实际成交价的止损止盈
    actual_stop = format_price(filled_price * (1 + STOP_LOSS_PERCENT), price_precision)
    actual_tp = format_price(filled_price * (1 - TAKE_PROFIT_PERCENT), price_precision)
    
    # 11. 设置止损
    sl_order = await set_stop_loss(filled_price, quantity, price_precision)
    if not sl_order:
        logger.error("止损单设置失败！请手动设置止损以防止亏损扩大！")
    
    # 12. 设置止盈
    tp_order = await set_take_profit(filled_price, quantity, price_precision)
    if not tp_order:
        logger.error("止盈单设置失败！请手动设置止盈！")
    
    # 13. 打印最终摘要
    print()
    print("=" * 60)
    print("  交易完成！最终摘要")
    print("=" * 60)
    print(f"  交易对:       {SYMBOL}")
    print(f"  方向:         做空")
    print(f"  入场价格:     {filled_price}")
    print(f"  开仓数量:     {quantity}")
    print(f"  杠杆倍数:     {LEVERAGE}x")
    print(f"  止损价格:     {actual_stop} (+{STOP_LOSS_PERCENT*100:.1f}%)")
    print(f"  止盈价格:     {actual_tp} (-{TAKE_PROFIT_PERCENT*100:.1f}%)")
    print(f"  订单ID:       {order.get('orderId')}")
    print(f"  订单状态:     {order.get('status')}")
    print("=" * 60)
    print()
    
    logger.info("BILLUSDT 手动做空脚本执行完毕！")
    
    if not sl_order or not tp_order:
        logger.warning("注意：止损或止盈设置不完整，请手动检查！")


def main():
    """入口函数"""
    parser = argparse.ArgumentParser(description="BILLUSDT 手动做空脚本")
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        dest="auto_confirm",
        default=False,
        help="自动确认，跳过交互确认步骤"
    )
    args = parser.parse_args()
    asyncio.run(run(auto_confirm=args.auto_confirm))


if __name__ == "__main__":
    main()