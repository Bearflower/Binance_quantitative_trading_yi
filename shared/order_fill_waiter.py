"""订单终态统一等待助手（R06）

所有「下单 → 等待 → 判定终态」的路径（入场等待、平仓等待、撤单后读最终量、
超时后按 clientOrderId 查单）统一走本模块，杜绝 btc_eth / btc_eth_aggressive /
new_coin 三处重复实现（架构 §2.2 核心抽象二）。

核心能力：
- 结构化返回 ``OrderFillResult``（替代语义贫乏的 ``Optional[Dict]``）；
- 识别「部分成交」（终态 CANCELED/EXPIRED/REJECTED 但 executedQty>0，R06-F2）；
- 超时撤单后重读最终成交量（R06-F3）；
- 撤单抛 -2011/-2013 竞态时按成交结果处理（R06-F4）；
- PM 账户首查可见延迟（``pm_order_visibility_delay_seconds``，配置化，R06-F6）；
- ``-2013`` 不可见在等待循环内消化重试，不落外层直接判「未成交」。

币安 PM 账户硬约束：下单后约百 ms 级 API 可见延迟，立即查单会报 ``[-2013]``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional

import structlog

from .api_retry_config import get_order_fill
from .binance_api import BinanceAPIError, BinanceClient
from .utils import to_decimal

logger = structlog.get_logger()

# OrderFillResult.status 取值范围（架构 §2.2）
STATUS_FILLED = "FILLED"
STATUS_PARTIAL = "PARTIAL"
STATUS_CANCELED_UNFILLED = "CANCELED_UNFILLED"
STATUS_EXPIRED_UNFILLED = "EXPIRED_UNFILLED"
STATUS_REJECTED = "REJECTED"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_FAILED = "FAILED"

# 撤单/查单竞态：订单已成交/不存在（R06-F4）
_ORDER_NOT_FOUND_CODES = (-2011, -2013)


@dataclass
class OrderFillResult:
    """订单终态统一表示（消除 ``Optional[Dict]`` 的语义贫乏）

    Attributes:
        status: FILLED | PARTIAL | CANCELED_UNFILLED | EXPIRED_UNFILLED |
            REJECTED | UNKNOWN | FAILED
        executed_qty: 累计已成成交量（对账后的权威值）
        orig_qty: 原始委托量
        remaining_qty: orig_qty - executed_qty（不小于 0）
        avg_price: 成交均价（部分成交时为已成交部分均价）
        order_id: 交易所订单号
        client_order_id: 客户端订单号
        raw: 交易所原始订单对象（审计用）
    """

    status: str = STATUS_UNKNOWN
    executed_qty: Decimal = Decimal("0")
    orig_qty: Decimal = Decimal("0")
    remaining_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    order_id: Optional[int] = None
    client_order_id: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        """是否完全成交（status==FILLED 且已成交量覆盖委托量）"""
        return self.status == STATUS_FILLED and self.executed_qty > 0 and self.remaining_qty <= 0

    @property
    def has_fill(self) -> bool:
        """是否存在成交（含部分成交，executed_qty > 0）"""
        return self.executed_qty > 0


def _build_result(order: Dict[str, Any]) -> OrderFillResult:
    """将交易所订单对象解析为 OrderFillResult（含部分成交识别）"""
    raw_status = str(order.get("status", "")).upper()
    executed = to_decimal(order.get("executedQty"))
    orig = to_decimal(order.get("origQty"))
    remaining = orig - executed
    if remaining < 0:
        remaining = Decimal("0")

    if raw_status == STATUS_FILLED:
        status = STATUS_FILLED
    elif raw_status == "CANCELED":
        status = STATUS_PARTIAL if executed > 0 else STATUS_CANCELED_UNFILLED
    elif raw_status == "EXPIRED":
        status = STATUS_PARTIAL if executed > 0 else STATUS_EXPIRED_UNFILLED
    elif raw_status == "REJECTED":
        # 文档 §2.2：REJECTED 视为未成交终态；若有成交量则按部分成交处理（R06-F2）
        status = STATUS_PARTIAL if executed > 0 else STATUS_REJECTED
    else:
        # NEW / PARTIALLY_FILLED / 未知原始状态 → 保守标记 UNKNOWN（不丢弃已成交量）
        status = STATUS_UNKNOWN

    return OrderFillResult(
        status=status,
        executed_qty=executed,
        orig_qty=orig,
        remaining_qty=remaining,
        avg_price=to_decimal(order.get("avgPrice")),
        order_id=order.get("orderId"),
        client_order_id=order.get("clientOrderId") or order.get("origClientOrderId"),
        raw=dict(order),
    )


async def _read_order_or_none(
    client: BinanceClient,
    symbol: str,
    order_id: Optional[int],
    client_order_id: Optional[str],
) -> Optional[Dict]:
    """查询订单；订单不存在（-2011/-2013，含可见延迟）返回 None，其他错误上抛"""
    try:
        if order_id is not None:
            return await client.get_order(symbol, order_id)
        return await client.get_order_by_client_id(symbol, client_order_id)
    except BinanceAPIError as e:
        if e.code in _ORDER_NOT_FOUND_CODES:
            return None
        raise


async def _cancel_quietly(
    client: BinanceClient,
    symbol: str,
    order_id: Optional[int],
    client_order_id: Optional[str],
) -> None:
    """超时撤单：-2011/-2013 竞态视为正常（交由后续读终态确认，R06-F4）"""
    if order_id is None and not client_order_id:
        return
    try:
        await client.cancel_order(symbol, order_id=order_id, client_order_id=client_order_id)
    except BinanceAPIError as e:
        if e.code in _ORDER_NOT_FOUND_CODES:
            logger.debug("撤单竞态（订单已成交/不存在），转读终态", symbol=symbol, code=e.code)
            return
        raise


async def wait_order_final_state(
    client: BinanceClient,
    symbol: str,
    *,
    order_id: Optional[int] = None,
    client_order_id: Optional[str] = None,
    timeout_seconds: float,
    check_interval: Optional[float] = None,
    visibility_delay: Optional[float] = None,
    final_read_retries: Optional[int] = None,
) -> OrderFillResult:
    """等待订单至终态并返回结构化结果（R06）

    Args:
        client: 币安客户端（需具备 get_order / get_order_by_client_id / cancel_order）
        symbol: 交易对
        order_id: 交易所订单号（与 client_order_id 至少提供其一）
        client_order_id: 客户端订单号
        timeout_seconds: 等待终态的超时时间（策略侧配置，如 btc_eth 300 / new_coin 60）
        check_interval: 轮询间隔（默认取 order_fill.check_interval_seconds）
        visibility_delay: 首查可见延迟（默认取 order_fill.pm_order_visibility_delay_seconds）
        final_read_retries: 撤单后读终态重试次数（默认取 order_fill.final_state_read_retries）

    Returns:
        OrderFillResult；超时后经撤单并重读最终量得出「未成交/部分成交/完全成交」。
    """
    if order_id is None and not client_order_id:
        raise ValueError("必须提供 order_id 或 client_order_id")

    interval = _resolve_check_interval(check_interval)
    delay = _resolve_visibility_delay(visibility_delay)
    read_retries = _resolve_read_retries(final_read_retries)

    # R06-F6：首循环前的小等，规避 PM 账户下单后的 API 可见延迟
    await asyncio.sleep(max(delay, 0.0))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(float(timeout_seconds), 0.0)
    while loop.time() < deadline:
        order = await _read_order_or_none(client, symbol, order_id, client_order_id)
        if order is None:
            # -2013 不可见：循环内消化重试（受 deadline 限制），不落外层判“未成交”
            await asyncio.sleep(max(interval, 0.0))
            continue
        result = _build_result(order)
        if result.status != STATUS_UNKNOWN:
            return result
        await asyncio.sleep(max(interval, 0.0))

    # 超时：撤单后读取最终成交量（R06-F3）
    logger.warning(
        "等待订单终态超时，撤单后读取最终成交量",
        symbol=symbol,
        order_id=order_id,
        client_order_id=client_order_id,
        timeout_seconds=timeout_seconds,
    )
    await _cancel_quietly(client, symbol, order_id, client_order_id)
    return await read_order_final_state(
        client,
        symbol,
        order_id=order_id,
        client_order_id=client_order_id,
        read_retries=read_retries,
        retry_interval=interval,
    )


async def read_order_final_state(
    client: BinanceClient,
    symbol: str,
    order_id: Optional[int] = None,
    *,
    client_order_id: Optional[str] = None,
    read_retries: Optional[int] = None,
    retry_interval: Optional[float] = None,
) -> OrderFillResult:
    """仅读订单终态（不等待），用于撤单后/竞态后确认最终 executedQty（R06-F3/F4）

    顺序读取若干次；若仍不可见（-2013 用尽）则保守返回 ``UNKNOWN``（携带原始对象
    供审计），交由上层告警，而非静默判定“未成交”。
    """
    if order_id is None and not client_order_id:
        raise ValueError("必须提供 order_id 或 client_order_id")

    attempts = max(_resolve_read_retries(read_retries), 1)
    interval = _resolve_check_interval(retry_interval)

    for index in range(attempts):
        order = await _read_order_or_none(client, symbol, order_id, client_order_id)
        if order is not None:
            return _build_result(order)
        if index < attempts - 1:
            await asyncio.sleep(max(interval, 0.0))

    logger.warning(
        "读取订单终态失败（不可见），返回 UNKNOWN",
        symbol=symbol,
        order_id=order_id,
        client_order_id=client_order_id,
    )
    return OrderFillResult(
        status=STATUS_UNKNOWN,
        order_id=order_id,
        client_order_id=client_order_id,
    )


def _resolve_check_interval(value: Optional[float]) -> float:
    """轮询间隔：显式入参优先，否则取共享配置默认值"""
    if value is not None:
        return float(value)
    return float(get_order_fill()["check_interval_seconds"])


def _resolve_visibility_delay(value: Optional[float]) -> float:
    """首查可见延迟：显式入参优先，否则取共享配置默认值（不得硬编码）"""
    if value is not None:
        return float(value)
    return float(get_order_fill()["pm_order_visibility_delay_seconds"])


def _resolve_read_retries(value: Optional[int]) -> int:
    """撤单后读终态重试次数：显式入参优先，否则取共享配置默认值"""
    if value is not None:
        return int(value)
    return int(get_order_fill()["final_state_read_retries"])