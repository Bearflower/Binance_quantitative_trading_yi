"""Verify a terminal entry order against complete, unique exchange trades."""
from decimal import Decimal, InvalidOperation


def _positive_decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("成交数值无效") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError("成交数值必须为有限正数")
    return result


def _positive_id(value):
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("成交标识无效") from exc
    if result <= 0 or str(result) != str(value):
        raise ValueError("成交标识无效")
    return result


def verified_fill(record, order, trades):
    """仅对完整且唯一标识的成交集合返回 Decimal 数量和成交量加权均价。

    调用方负责将 API 凭据和数据库记录绑定至同一账户。
    满页结果无法证明后续成交没有被截断，因此拒绝用于回填。
    """
    if not isinstance(order, dict) or order.get("status") != "FILLED":
        raise ValueError("terminal fill unavailable")
    order_id = _positive_id(record["order_id"])
    symbol, side = record["symbol"], record["side"]
    if (not symbol or side not in ("BUY", "SELL")
            or _positive_id(order.get("orderId")) != order_id
            or order.get("symbol") != symbol or order.get("side") != side):
        raise ValueError("order identity mismatch")
    executed = _positive_decimal(order.get("executedQty"))
    if _positive_decimal(order.get("origQty")) != executed:
        raise ValueError("订单数量不匹配")
    if not isinstance(trades, list) or not 0 < len(trades) < 1000:
        raise ValueError("complete fill page unavailable")
    quantity = Decimal("0")
    quote = Decimal("0")
    seen = set()
    for trade in trades:
        if not isinstance(trade, dict):
            raise ValueError("成交记录无效")
        trade_id = _positive_id(trade.get("id"))
        if trade_id in seen:
            raise ValueError("missing or duplicate trade")
        if (_positive_id(trade.get("orderId")) != order_id
                or trade.get("symbol") != symbol or trade.get("side") != side):
            raise ValueError("trade identity mismatch")
        seen.add(trade_id)
        qty = _positive_decimal(trade.get("qty"))
        price = _positive_decimal(trade.get("price"))
        quantity += qty
        quote += qty * price
    if quantity != executed:
        raise ValueError("incomplete executed quantity")
    return quantity, quote / quantity
