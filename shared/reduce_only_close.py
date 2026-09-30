"""统一减仓平仓助手（R03 + R04-F3，架构 §2.2 核心抽象三）

「读真实持仓 → 算剩余待平量 → 带 reduceOnly 提交 → -2022 前置对账/撤同向条件单
→ 撤单后读最终量累加 → 每轮对账防反向 → 剩余量为 0/达标且对账通过才返回成功」
统一走本模块，供 R03 / R04 / R05 减仓兜底复用，杜绝策略内重复实现。

用户定论：
- **D1**：平仓单强制带 ``reduceOnly``；被拒 ``[-2022]`` 时先对账 / 先撤同向条件单再重试。
- **D3**：剩余量因精度截断小于最小下单量或为 0 → 减仓清零（平掉微仓）；清零失败则告警。

币安 PM 账户硬约束（实现依据）：
1. 限价条件单 STOP/TAKE_PROFIT **不支持** ``closePosition=true``；仅市价条件单
   STOP_MARKET/TAKE_PROFIT_MARKET 支持。
2. 平仓 ``ReduceOnly`` 单可能报 ``[-2022]``，须先对账真实持仓或先撤同向条件单，
   再提交平仓单，避免 ``[-4118]`` / ``[-4130]``。
3. 下单后约百 ms 级 API 可见延迟，立即查单会报 ``[-2013]``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Dict, Optional, Tuple

import structlog

from .api_retry_config import get_order_fill
from .binance_api import BinanceAPIError, BinanceClient, UnknownOrderResultError
from .order_fill_waiter import STATUS_UNKNOWN, OrderFillResult, wait_order_final_state
from .utils import to_decimal

logger = structlog.get_logger()

# CloseOutcome.status 取值范围：完全成交 / 部分成交 / 已受理未成交 / 失败
CLOSE_FILLED = "FILLED"
CLOSE_PARTIAL = "PARTIAL"
CLOSE_ACCEPTED = "ACCEPTED"
CLOSE_FAILED = "FAILED"

# PM 账户 ReduceOnly 被拒错误码（D1）
_REDUCE_ONLY_REJECT_CODES = (-2022,)
# 最小下单量兜底步长（交易所精度缺失时的降级值，与既有实现保持一致）
_DEFAULT_STEP_SIZE = "0.001"


@dataclass
class CloseOutcome:
    """减仓平仓结果（供策略侧判定「受理/部分成交/完全成交/失败」）

    Attributes:
        status: FILLED | PARTIAL | ACCEPTED | FAILED
        closed_qty: 累计已减仓量（对账后的权威值）
        target_qty: 本次目标减仓量（已按 stepSize 截断）
        success: 是否达成目标且持仓对账通过
        reason: 中文结果说明
        raw: 最后一笔订单原始对象（审计用，可为 None）
    """

    status: str = CLOSE_FAILED
    closed_qty: Decimal = Decimal("0")
    target_qty: Decimal = Decimal("0")
    success: bool = False
    reason: str = ""
    raw: Optional[Dict[str, Any]] = field(default=None)


@dataclass
class _CloseParams:
    """减仓过程的内部参数集合（避免长参数列表在各辅助函数间反复传递）"""

    symbol: str
    close_side: str
    step: Decimal
    order_type: str
    price: Optional[Decimal]
    reduce_only: bool
    sync_before_reduce_only: bool
    cancel_conditional_on_reject: bool
    zero_micro_position: bool
    max_retries: int
    retry_interval: float
    poll_interval: float
    timeout_seconds: float
    confirm_retries: int
    confirm_interval: float


async def close_remaining(
    client: BinanceClient,
    symbol: str,
    target_qty: Decimal,
    *,
    side: Optional[str] = None,
    order_type: str = "MARKET",
    price: Optional[Decimal] = None,
    reduce_only: bool = True,
    sync_before_reduce_only: bool = True,
    step_size: Optional[str] = None,
    max_retries: int = 3,
    retry_interval: float = 2.0,
    poll_interval: Optional[float] = None,
    timeout_seconds: float = 10.0,
    position_confirm_retries: int = 2,
    position_confirm_interval: float = 1.0,
    zero_micro_position: bool = True,
    cancel_conditional_on_reject: bool = True,
) -> CloseOutcome:
    """按「剩余待平量」带减仓约束（reduceOnly）平仓，直到达标或耗尽重试

    Args:
        client: 币安客户端（需具备 get_position / get_symbol_info / place_order /
            get_order / cancel_order；PM 账户 -2022 处理会调用 cancel_all_algo_orders）
        symbol: 交易对
        target_qty: 目标减仓量（正数；实际提交量不超过当前真实可平量，防反向）
        side: 平仓方向（BUY 平空 / SELL 平多）；不传则按真实持仓符号自动推断
        order_type: 订单类型（默认 MARKET，市价兜底）
        price: 限价单价格（order_type=LIMIT 时必填）
        reduce_only: 是否对平仓单加减仓约束（D1，默认 True）
        sync_before_reduce_only: -2022 时是否先对账/撤同向条件单（D1，默认 True）
        step_size: 数量步长（不传则从交易所精度信息读取）
        max_retries: 最大重试次数（每轮重新读取真实持仓并重算剩余量）
        retry_interval: 两轮之间的间隔（秒）
        poll_interval: 订单终态轮询间隔（不传则取 order_fill.check_interval_seconds）
        timeout_seconds: 单笔减仓单等待终态超时（秒）
        position_confirm_retries: 持仓对账重试次数
        position_confirm_interval: 持仓对账间隔（秒）
        zero_micro_position: 剩余量精度截断为 0 时是否减仓清零（D3，默认 True）
        cancel_conditional_on_reject: -2022 时是否撤该币种条件单以解除冲突

    Returns:
        CloseOutcome（status/closed_qty/target_qty/success/reason/raw）
    """
    if not symbol or not symbol.strip():
        raise ValueError("交易对不能为空")
    symbol = symbol.strip().upper()
    if target_qty is None or target_qty <= 0:
        raise ValueError(f"目标平仓量必须大于0: {target_qty}")

    step = await _resolve_step_size(client, symbol, step_size)
    initial = await _read_position_qty(client, symbol)
    if initial == 0:
        return CloseOutcome(CLOSE_FILLED, Decimal("0"), Decimal("0"), True, "当前无持仓，无需减仓")

    close_side = side or ("SELL" if initial > 0 else "BUY")
    if close_side not in ("BUY", "SELL"):
        raise ValueError(f"无效的平仓方向: {close_side}")

    params = _CloseParams(
        symbol=symbol,
        close_side=close_side,
        step=step,
        order_type=order_type,
        price=price,
        reduce_only=reduce_only,
        sync_before_reduce_only=sync_before_reduce_only,
        cancel_conditional_on_reject=cancel_conditional_on_reject,
        zero_micro_position=zero_micro_position,
        max_retries=max(int(max_retries), 0),
        retry_interval=float(retry_interval),
        poll_interval=_resolve_poll_interval(poll_interval),
        timeout_seconds=float(timeout_seconds),
        confirm_retries=max(int(position_confirm_retries), 1),
        confirm_interval=float(position_confirm_interval),
    )

    # 目标量以「初始真实持仓」为上限，并按 stepSize 向下截断（R03-F5 / AC5 防反向）
    target = _floor_to_step(min(abs(target_qty), abs(initial)), step)
    if target <= 0:
        return await _zero_micro_position(client, params, initial)
    return await _close_loop(client, params, target)


async def _close_loop(client: BinanceClient, p: _CloseParams, target: Decimal) -> CloseOutcome:
    """主循环：每轮重读真实持仓、重算剩余量、带 reduceOnly 提交并累加最终成交量"""
    closed = Decimal("0")
    last_status = CLOSE_FAILED
    last_raw: Optional[Dict[str, Any]] = None

    for _ in range(p.max_retries + 1):
        remaining = target - closed
        if remaining <= 0:
            break
        current = await _read_position_qty(client, p.symbol)
        if current == 0 or not _direction_matches(current, p.close_side):
            break
        submit_qty = _floor_to_step(min(remaining, abs(current)), p.step)
        if submit_qty <= 0:
            break
        result = await _submit_and_wait(client, p, submit_qty)
        if result is not None:
            last_status = _outcome_status(result, submit_qty)
            last_raw = result.raw
            closed += result.executed_qty
        else:
            last_status = CLOSE_FAILED
        is_flat, _ = await _confirm_flat(client, p.symbol, p.confirm_retries, p.confirm_interval)
        if is_flat:
            break
        await asyncio.sleep(max(p.retry_interval, 0.0))

    return await _finalize_outcome(client, p, target, closed, last_status, last_raw)


async def _finalize_outcome(
    client: BinanceClient,
    p: _CloseParams,
    target: Decimal,
    closed: Decimal,
    last_status: str,
    last_raw: Optional[Dict[str, Any]],
) -> CloseOutcome:
    """成功语义收紧（R03-F5）：仅当达标且持仓对账通过才返回成功"""
    is_flat, current = await _confirm_flat(client, p.symbol, p.confirm_retries, p.confirm_interval)
    if closed >= target and (is_flat or abs(current) <= p.step):
        return CloseOutcome(CLOSE_FILLED, closed, target, True,
                            f"减仓达标（closed={closed}, target={target}）", last_raw)
    if closed > 0:
        return CloseOutcome(CLOSE_PARTIAL, closed, target, False,
                            f"部分减仓，剩余待平 {target - closed}（真实持仓 {current}）", last_raw)
    if last_status == CLOSE_ACCEPTED:
        return CloseOutcome(CLOSE_ACCEPTED, closed, target, False, "平仓单已受理但未成交", last_raw)
    return CloseOutcome(CLOSE_FAILED, closed, target, False, f"减仓未成交（真实持仓 {current}）", last_raw)


async def _submit_and_wait(
    client: BinanceClient, p: _CloseParams, qty: Decimal
) -> Optional[OrderFillResult]:
    """提交减仓单并等待终态（结果未知时不重发，交下一轮持仓对账决定）"""
    try:
        order = await _submit_reduce_only(client, p, qty)
    except UnknownOrderResultError as e:
        logger.warning("减仓单结果未知，转由下一轮持仓对账决定（reduceOnly 防反向）",
                       symbol=p.symbol, error=str(e))
        return None
    if order is None:
        return None

    order_id = order.get("orderId")
    client_order_id = order.get("clientOrderId")
    if order_id is None and not client_order_id:
        logger.warning("减仓单响应缺少订单标识，无法确认终态", symbol=p.symbol, raw=order)
        return None
    return await wait_order_final_state(
        client, p.symbol,
        order_id=order_id, client_order_id=client_order_id,
        timeout_seconds=p.timeout_seconds, check_interval=p.poll_interval,
    )


async def _submit_reduce_only(
    client: BinanceClient, p: _CloseParams, qty: Decimal, *, retried: bool = False
) -> Optional[Dict]:
    """提交减仓单；-2022 被拒时（D1）先对账/撤同向单后按剩余量重试一次"""
    try:
        return await client.place_order(
            p.symbol, p.close_side, quantity=qty, price=p.price,
            order_type=p.order_type, reduce_only=p.reduce_only,
        )
    except BinanceAPIError as e:
        if e.code in _REDUCE_ONLY_REJECT_CODES and p.sync_before_reduce_only and not retried:
            await _handle_reduce_only_rejection(client, p)
            return await _submit_reduce_only(client, p, qty, retried=True)
        logger.error("减仓单提交被拒", symbol=p.symbol, code=e.code, message=e.message)
        return None


async def _handle_reduce_only_rejection(client: BinanceClient, p: _CloseParams) -> None:
    """-2022 前置处理（D1）：先对账真实持仓，必要时撤该币种条件单以解除冲突"""
    current = await _read_position_qty(client, p.symbol)
    logger.warning("减仓单被拒(-2022)，已对账真实持仓", symbol=p.symbol, position_amt=str(current))
    if not p.cancel_conditional_on_reject:
        return
    try:
        await client.cancel_all_algo_orders(p.symbol)
        logger.warning("已撤该币种全部条件单以解除减仓冲突", symbol=p.symbol)
    except Exception as e:
        # 撤单失败不阻塞减仓重试（条件单仅影响 -4118/-4130，非致命）
        logger.warning("撤同向条件单失败（继续重试减仓单）", symbol=p.symbol, error=str(e))


async def _zero_micro_position(
    client: BinanceClient, p: _CloseParams, initial: Decimal
) -> CloseOutcome:
    """D3：剩余量经精度截断为 0 但仍有持仓 → 减仓清零（平掉微仓）；失败则告警"""
    if not p.zero_micro_position:
        msg = f"剩余量经精度截断为 0 且仍有持仓 {initial}（需人工处置）"
        logger.warning("减仓清零已禁用", symbol=p.symbol, reason=msg)
        return CloseOutcome(CLOSE_FAILED, Decimal("0"), Decimal("0"), False, msg)

    micro = _ceil_to_step(abs(initial), p.step)
    if micro <= 0:
        logger.warning("微仓低于最小下单量，无法清零", symbol=p.symbol, position_amt=str(initial))
        return CloseOutcome(CLOSE_FAILED, Decimal("0"), Decimal("0"), False, "微仓低于最小下单量，无法清零")

    result = await _submit_and_wait(client, p, micro)
    closed = result.executed_qty if result is not None else Decimal("0")
    is_flat, current = await _confirm_flat(client, p.symbol, p.confirm_retries, p.confirm_interval)
    if is_flat:
        raw = result.raw if result is not None else None
        return CloseOutcome(CLOSE_FILLED, closed, micro, True, "微仓已清零", raw)
    logger.warning("微仓减仓清零失败（已告警）", symbol=p.symbol, remaining=str(current))
    return CloseOutcome(CLOSE_FAILED, closed, micro, False, f"微仓清零失败，剩余 {current}")


def _outcome_status(result: OrderFillResult, submit_qty: Decimal) -> str:
    """按单笔减仓单的成交结果分类（供最终状态兜底判定）"""
    if result.executed_qty >= submit_qty:
        return CLOSE_FILLED
    if result.executed_qty > 0:
        return CLOSE_PARTIAL
    if result.status == STATUS_UNKNOWN:
        return CLOSE_ACCEPTED
    return CLOSE_FAILED


async def _resolve_step_size(
    client: BinanceClient, symbol: str, step_size: Optional[str]
) -> Decimal:
    """解析数量步长：显式入参优先，否则读取交易所精度信息"""
    if step_size is not None:
        return to_decimal(step_size)
    info = await client.get_symbol_info(symbol)
    return to_decimal(info.get("stepSize") or _DEFAULT_STEP_SIZE)


async def _read_position_qty(client: BinanceClient, symbol: str) -> Decimal:
    """读取交易所真实持仓数量（带符号：正=多头，负=空头）"""
    positions = await client.get_position(symbol)
    return _extract_position_amt(positions, symbol)


async def _confirm_flat(
    client: BinanceClient, symbol: str, retries: int, interval: float
) -> Tuple[bool, Decimal]:
    """反复对账真实持仓，判断是否已归零（应对交易所对账延迟）"""
    current = Decimal("0")
    attempts = max(int(retries), 1)
    for index in range(attempts):
        current = await _read_position_qty(client, symbol)
        if current == 0:
            return True, Decimal("0")
        if index < attempts - 1:
            await asyncio.sleep(max(float(interval), 0.0))
    return False, current


def _extract_position_amt(positions: Any, symbol: str) -> Decimal:
    """从持仓列表中提取指定交易对的 positionAmt"""
    if not positions:
        return Decimal("0")
    for item in positions:
        if item.get("symbol") == symbol:
            return to_decimal(item.get("positionAmt"))
    return Decimal("0")


def _direction_matches(current: Decimal, close_side: str) -> bool:
    """真实持仓方向与减仓方向是否匹配（防反向）：多头只能 SELL 平，空头只能 BUY 平"""
    return (current > 0 and close_side == "SELL") or (current < 0 and close_side == "BUY")


def _floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    """向下取整到 stepSize 整数倍（保证提交量不超过可平量，防反向）"""
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil_to_step(qty: Decimal, step: Decimal) -> Decimal:
    """向上取整到 stepSize 整数倍（微仓清零时保证不小于最小下单量）"""
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_CEILING) * step


def _resolve_poll_interval(value: Optional[float]) -> float:
    """订单终态轮询间隔：显式入参优先，否则取共享配置默认值"""
    if value is not None:
        return float(value)
    return float(get_order_fill()["check_interval_seconds"])