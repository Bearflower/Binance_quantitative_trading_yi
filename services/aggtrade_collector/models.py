"""聚合成交流的数据归一化与字段校验（REST 响应 / 历史样本文件共用）。

字段映射（币安 aggTrades，需求 §5.1 成交价口径）：
    a -> agg_trade_id    p -> price       q -> quantity
    f -> first_trade_id  l -> last_trade_id
    T -> trade_time_ms   m -> is_buyer_maker
price/quantity 保持 Decimal 字符串入库，不做浮点转换。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict

# REST 响应字段 -> 库内字段
REST_FIELD_MAP = {
    "a": "agg_trade_id",
    "p": "price",
    "q": "quantity",
    "f": "first_trade_id",
    "l": "last_trade_id",
    "T": "trade_time_ms",
    "m": "is_buyer_maker",
}

# 历史样本文件字段 -> 库内字段（trade_time_beijing 仅展示用，不入库）
FILE_FIELD_MAP = {
    "aggTradeId": "agg_trade_id",
    "price": "price",
    "quantity": "quantity",
    "firstTradeId": "first_trade_id",
    "lastTradeId": "last_trade_id",
    "trade_time_ms": "trade_time_ms",
    "is_buyer_maker": "is_buyer_maker",
}

_INSERT_COLUMNS = (
    "agg_trade_id", "symbol", "price", "quantity", "first_trade_id",
    "last_trade_id", "trade_time_ms", "is_buyer_maker", "received_at_ms",
)


def _to_int(value: Any, field: str) -> int:
    """严格转 int：拒绝 bool/浮点字符串/非整数。"""
    if isinstance(value, bool):
        raise ValueError(f"{field} 不能是布尔值: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"{field} 必须为整数，实际: {value!r}")


def _to_maker_flag(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1):
        return int(value)
    raise ValueError(f"is_buyer_maker 必须为 0/1 或布尔值，实际: {value!r}")


def _check_positive_decimal(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须为非空数字字符串，实际: {value!r}")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{field} 不是合法 Decimal: {value!r}")
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} 必须为有限正数，实际: {value!r}")
    return value.strip()


def normalize_trade(raw: Dict[str, Any], field_map: Dict[str, str],
                   symbol: str, received_at_ms: int) -> Dict[str, Any]:
    """按字段映射归一化单行并做边界校验（入库前唯一校验点）。"""
    missing = [src for src in field_map if src not in raw]
    if missing:
        raise ValueError(f"成交记录缺字段: {missing}; 原始记录: {raw}")
    trade = {dst: raw[src] for src, dst in field_map.items()}
    trade["agg_trade_id"] = _to_int(trade["agg_trade_id"], "agg_trade_id")
    trade["first_trade_id"] = _to_int(trade["first_trade_id"], "first_trade_id")
    trade["last_trade_id"] = _to_int(trade["last_trade_id"], "last_trade_id")
    trade["trade_time_ms"] = _to_int(trade["trade_time_ms"], "trade_time_ms")
    trade["is_buyer_maker"] = _to_maker_flag(trade["is_buyer_maker"])
    trade["price"] = _check_positive_decimal(trade["price"], "price")
    trade["quantity"] = _check_positive_decimal(trade["quantity"], "quantity")
    if trade["agg_trade_id"] <= 0 or trade["trade_time_ms"] <= 0:
        raise ValueError(f"agg_trade_id/trade_time_ms 必须为正数: {trade}")
    if trade["first_trade_id"] > trade["last_trade_id"]:
        raise ValueError(f"first_trade_id 不得大于 last_trade_id: {trade}")
    trade["symbol"] = symbol
    trade["received_at_ms"] = received_at_ms
    return trade


def insert_params(trade: Dict[str, Any]) -> tuple:
    """按固定列序生成入库参数。"""
    return tuple(trade[name] for name in _INSERT_COLUMNS)
