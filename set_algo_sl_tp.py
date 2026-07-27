#!/usr/bin/env python3
"""
BILLUSDT 仓位止盈止损条件单设置脚本
使用币安统一账户接口 POST /papi/v1/um/algo/order

关键发现：
  - 参数名是 triggerPrice（不是 stopPrice！）
  - algoType=CONDITIONAL 是必填参数
  - 止损类型: STOP_MARKET（价格 >= triggerPrice 触发）
  - 止盈类型: TAKE_PROFIT_MARKET（价格 <= triggerPrice 触发）
  - 做空平仓 = BUY，做多平仓 = SELL

接口文档：https://binance-docs.github.io/apidocs/pm/en/introduction
"""

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

import requests


# ============================================================
# 配置区（从环境变量读取，避免硬编码）
# ============================================================
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BASE_URL = "https://papi.binance.com"

# ---- 以下参数根据仓位实际情况修改 ----
SYMBOL = "BILLUSDT"
ALGO_TYPE = "CONDITIONAL"           # 条件单类型（必填）
WORKING_TYPE = "CONTRACT_PRICE"     # 使用合约价格触发

# 止损参数（做空：BUY STOP_MARKET，价格 >= triggerPrice 触发）
SL_TRIGGER_PRICE = 0.12387          # 止损触发价
SL_ORDER_TYPE = "STOP_MARKET"

# 止盈参数（做空：BUY TAKE_PROFIT_MARKET，价格 <= triggerPrice 触发）
TP_TRIGGER_PRICE = 0.10617          # 止盈触发价
TP_ORDER_TYPE = "TAKE_PROFIT_MARKET"

# 仓位参数
POSITION_QTY = 423                  # 仓位数量
CLOSE_SIDE = "BUY"                  # 做空平仓=BUY；做多平仓=SELL


def generate_signature(query_string: str) -> str:
    """生成 HMAC-SHA256 签名"""
    return hmac.new(
        API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def build_signed_params(params: Dict[str, Any]) -> str:
    """构建带签名的查询参数字符串"""
    params["timestamp"] = int(time.time() * 1000)
    sorted_keys = sorted(params.keys())
    query_string = "&".join([f"{k}={params[k]}" for k in sorted_keys])
    signature = generate_signature(query_string)
    return f"{query_string}&signature={signature}"


def query_position(symbol: str = SYMBOL) -> Optional[Dict[str, Any]]:
    """
    查询当前仓位信息
    GET /papi/v1/um/positionRisk
    """
    print(f"\n{'='*60}")
    print(f"  查询 {symbol} 当前仓位")
    print(f"{'='*60}")

    params = {"symbol": symbol}
    signed_params = build_signed_params(params)
    url = f"{BASE_URL}/papi/v1/um/positionRisk"

    try:
        headers = {"X-MBX-APIKEY": API_KEY}
        response = requests.get(url, params=signed_params, headers=headers, timeout=30)
        print(f"  [响应] HTTP {response.status_code}")

        if response.status_code == 200:
            positions = response.json()
            for p in positions:
                pos_amt = float(p.get("positionAmt", "0"))
                if abs(pos_amt) > 0:
                    direction = "做多 LONG" if pos_amt > 0 else "做空 SHORT"
                    print(f"  [仓位] {direction} | 数量={abs(pos_amt)} | 入场价={p.get('entryPrice')}")
                    print(f"  [仓位] 标记价={p.get('markPrice')} | 未实现盈亏={p.get('unRealizedProfit')}")
                    return p
            print(f"  [结果] {symbol} 无持仓")
            return None
        else:
            result = response.json()
            print(f"  [失败] {result.get('msg', '未知错误')}")
            return None
    except Exception as e:
        print(f"  [异常] {e}")
        return None


def send_algo_order(
    order_type: str,
    trigger_price: float,
    quantity: int,
    order_desc: str,
) -> Optional[Dict[str, Any]]:
    """
    发送算法条件单
    POST /papi/v1/um/algo/order

    Args:
        order_type: 订单类型 (STOP_MARKET / TAKE_PROFIT_MARKET)
        trigger_price: 触发价格（注意：参数名是 triggerPrice，不是 stopPrice）
        quantity: 数量
        order_desc: 订单描述（用于日志）

    Returns:
        订单响应字典，失败返回 None
    """
    print(f"\n{'='*60}")
    print(f"  发送{order_desc}条件单")
    print(f"{'='*60}")

    params = {
        "symbol": SYMBOL,
        "side": CLOSE_SIDE,
        "type": order_type,
        "quantity": str(quantity),
        "triggerPrice": str(trigger_price),
        "algoType": ALGO_TYPE,
        "workingType": WORKING_TYPE,
        "reduceOnly": "TRUE",
    }

    signed_params = build_signed_params(params)
    url = f"{BASE_URL}/papi/v1/um/algo/order"

    print(f"  [请求] POST {url}")
    print(f"  [参数] symbol={SYMBOL}, side={CLOSE_SIDE}, type={order_type}")
    print(f"  [参数] quantity={quantity}, triggerPrice={trigger_price}")
    print(f"  [参数] algoType={ALGO_TYPE}, workingType={WORKING_TYPE}, reduceOnly=TRUE")

    try:
        headers = {
            "X-MBX-APIKEY": API_KEY,
            "Content-Type": "application/x-www-form-urlencoded"
        }
        response = requests.post(
            url,
            data=signed_params,
            headers=headers,
            timeout=30
        )

        print(f"  [响应] HTTP {response.status_code}")

        if response.status_code == 200:
            result = response.json()
            algo_id = result.get("algoId", "N/A")
            algo_status = result.get("algoStatus", "N/A")
            actual_trigger = result.get("triggerPrice", "N/A")
            print(f"  [成功] algoId={algo_id} | status={algo_status} | triggerPrice={actual_trigger}")
            return result
        else:
            try:
                result = response.json()
            except json.JSONDecodeError:
                result = {"raw": response.text[:500]}
            error_msg = result.get("msg", "未知错误")
            error_code = result.get("code", "N/A")
            print(f"  [失败] code={error_code}, msg={error_msg}")
            return None

    except requests.exceptions.RequestException as e:
        print(f"  [异常] 网络请求失败: {e}")
        return None


def validate_config() -> bool:
    """验证配置是否完整"""
    if not API_KEY or not API_SECRET:
        print("❌ 错误：缺少 API 密钥配置！")
        print("  请设置环境变量 BINANCE_API_KEY 和 BINANCE_API_SECRET")
        return False
    return True


def main():
    """主函数：依次查询仓位、设置止损和止盈条件单"""
    print("=" * 60)
    print(f"  {SYMBOL} 仓位止盈止损条件单设置工具")
    print("=" * 60)
    print(f"  交易对:       {SYMBOL}")
    print(f"  期望仓位方向: 做空（SELL），平仓方向=BUY")
    print(f"  期望仓位数量: {POSITION_QTY}")
    print(f"  止损触发价:   {SL_TRIGGER_PRICE}（价格上涨时买入平仓）")
    print(f"  止盈触发价:   {TP_TRIGGER_PRICE}（价格下跌时买入平仓）")
    print(f"  API 基础URL:  {BASE_URL}")
    print(f"  API 端点:     /papi/v1/um/algo/order")

    # 验证配置
    if not validate_config():
        return

    # 第一步：查询当前仓位
    print(f"\n{'='*60}")
    print(f"  第一步：查询当前仓位")
    print(f"{'='*60}")
    position = query_position()

    if position is None:
        print("\n⚠️  警告：无法查询仓位，将继续设置条件单...")
    else:
        pos_amt = float(position.get("positionAmt", "0"))
        mark_price = float(position.get("markPrice", "0"))
        print(f"\n  [重要] 当前标记价: {mark_price}")

        # 检查止损触发价是否合理
        if SL_ORDER_TYPE == "STOP_MARKET":
            if CLOSE_SIDE == "BUY" and mark_price >= SL_TRIGGER_PRICE:
                print(f"  ⚠️  当前价 {mark_price} >= 止损触发价 {SL_TRIGGER_PRICE}")
                print(f"  ⚠️  止损单会立即触发！请调整 triggerPrice 到高于 {mark_price}")
            elif CLOSE_SIDE == "SELL" and mark_price <= SL_TRIGGER_PRICE:
                print(f"  ⚠️  当前价 {mark_price} <= 止损触发价 {SL_TRIGGER_PRICE}")
                print(f"  ⚠️  止损单会立即触发！请调整 triggerPrice 到低于 {mark_price}")

        # 检查止盈触发价是否合理
        if TP_ORDER_TYPE == "TAKE_PROFIT_MARKET":
            if CLOSE_SIDE == "BUY" and mark_price <= TP_TRIGGER_PRICE:
                print(f"  ⚠️  当前价 {mark_price} <= 止盈触发价 {TP_TRIGGER_PRICE}")
                print(f"  ⚠️  止盈单会立即触发！请调整 triggerPrice 到低于 {mark_price}")
            elif CLOSE_SIDE == "SELL" and mark_price >= TP_TRIGGER_PRICE:
                print(f"  ⚠️  当前价 {mark_price} >= 止盈触发价 {TP_TRIGGER_PRICE}")
                print(f"  ⚠️  止盈单会立即触发！请调整 triggerPrice 到高于 {mark_price}")

    # 第二步：设置止损单
    sl_result = send_algo_order(SL_ORDER_TYPE, SL_TRIGGER_PRICE, POSITION_QTY, "止损")

    # 第三步：设置止盈单
    tp_result = send_algo_order(TP_ORDER_TYPE, TP_TRIGGER_PRICE, POSITION_QTY, "止盈")

    # 第四步：汇总报告
    print(f"\n{'='*60}")
    print(f"  执行汇总")
    print(f"{'='*60}")
    sl_ok = sl_result is not None
    tp_ok = tp_result is not None
    print(f"  止损单: {'✅ 成功' if sl_ok else '❌ 失败'}")
    if sl_ok:
        print(f"    algoId={sl_result.get('algoId')} | triggerPrice={sl_result.get('triggerPrice')}")
    print(f"  止盈单: {'✅ 成功' if tp_ok else '❌ 失败'}")
    if tp_ok:
        print(f"    algoId={tp_result.get('algoId')} | triggerPrice={tp_result.get('triggerPrice')}")

    if sl_ok and tp_ok:
        print(f"\n  注：条件单已在币安服务器端管理，不受本地进程影响")
        print(f"  可以安全杀掉本地监控: pkill -f monitor_sl_tp")
    else:
        print(f"\n  ⚠️  部分订单设置失败，请检查上述错误信息！")


if __name__ == "__main__":
    main()