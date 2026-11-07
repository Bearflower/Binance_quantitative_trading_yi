"""Order-level proof for an executed PM UM conditional STOP.

This module does not assign the execution to a position lot or change trading
frequency state. Those require separate durable ownership evidence.
"""

from decimal import Decimal, InvalidOperation


def _positive_id(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 and str(number) == str(value) else None


def _amount(value, *, allow_negative=False, allow_zero=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or (number < 0 and not allow_negative) or (number == 0 and not allow_zero):
        return None
    return number


def verified_stop_execution(account_id, symbol, algo_id, algo, order, fills):
    """Return audited order-level facts or None; never infer strategy/lot ownership."""
    if (not account_id or not symbol or not isinstance(algo, dict)
            or not isinstance(order, dict) or not isinstance(fills, list) or not fills):
        return None
    expected_algo = _positive_id(algo_id)
    actual_order = _positive_id(algo.get("actualOrderId"))
    side = algo.get("side")
    if (expected_algo is None or _positive_id(algo.get("algoId")) != expected_algo
            or algo.get("symbol") != symbol or algo.get("orderType") not in ("STOP", "STOP_MARKET")
            or algo.get("algoStatus") not in ("TRIGGERED", "FINISHED")
            or side not in ("BUY", "SELL") or actual_order is None):
        return None
    reduce_only = order.get("reduceOnly")
    if (order.get("symbol") != symbol or _positive_id(order.get("orderId")) != actual_order
            or order.get("status") != "FILLED" or order.get("side") != side
            or not (reduce_only is True or (isinstance(reduce_only, str) and reduce_only == "true"))):
        return None
    executed = _amount(order.get("executedQty"))
    if executed is None:
        return None
    quantity = Decimal(0)
    pnl = Decimal(0)
    commission = Decimal(0)
    trade_ids = set()
    last_time = 0
    for fill in fills:
        if not isinstance(fill, dict):
            return None
        trade_id = _positive_id(fill.get("id"))
        fill_time = _positive_id(fill.get("time"))
        qty = _amount(fill.get("qty"))
        realized = _amount(fill.get("realizedPnl"), allow_negative=True, allow_zero=True)
        fee = _amount(fill.get("commission"), allow_zero=True)
        if (trade_id is None or trade_id in trade_ids or fill_time is None
                or _positive_id(fill.get("orderId")) != actual_order
                or fill.get("symbol") != symbol or fill.get("side") != side
                or fill.get("commissionAsset") != "USDT"
                or qty is None or realized is None or fee is None):
            return None
        trade_ids.add(trade_id)
        last_time = max(last_time, fill_time)
        quantity += qty
        pnl += realized
        commission += fee
    if quantity != executed:
        return None
    return {"account_id": account_id, "symbol": symbol, "algo_id": expected_algo,
            "order_id": actual_order, "trade_ids": sorted(trade_ids), "side": side,
            "quantity": str(quantity), "realized_pnl": str(pnl),
            "commission": str(commission), "commission_asset": "USDT",
            "last_fill_time_ms": last_time}


async def fetch_verified_stop_execution(client, account_id, symbol, algo_id):
    """Read a STOP's triggered order and fills; return only complete order-level proof.

    A full 1000-fill response has an unknown next page. It is deliberately
    rejected until pagination can prove the order has no more fills.
    """
    expected_algo = _positive_id(algo_id)
    if not account_id or not symbol or expected_algo is None:
        return None
    algo = await client.get_algo_order(expected_algo)
    if (not isinstance(algo, dict) or _positive_id(algo.get("algoId")) != expected_algo
            or algo.get("symbol") != symbol or algo.get("orderType") not in ("STOP", "STOP_MARKET")
            or algo.get("algoStatus") not in ("TRIGGERED", "FINISHED")):
        return None
    order_id = _positive_id(algo.get("actualOrderId"))
    if order_id is None:
        return None
    order = await client.get_order(symbol, order_id)
    if not isinstance(order, dict) or _positive_id(order.get("orderId")) != order_id:
        return None
    fills = await client.get_user_trades(symbol, order_id=str(order_id), limit=1000)
    if not isinstance(fills, list) or len(fills) >= 1000:
        return None
    return verified_stop_execution(account_id, symbol, expected_algo, algo, order, fills)
