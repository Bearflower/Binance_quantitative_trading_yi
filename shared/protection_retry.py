"""交易执行加固共享模块（R05 / R06 / R07）

背景：本批「R01–R08 交易安全修复」中，R05（保护单失败不丢态 + 补挂）、R06（入场
等待统一助手）、R07（归属占用）在 ``strategies/btc_eth/strategy.py`` 与
``strategies/btc_eth_aggressive/strategy.py`` 两处形成了高度同构的重复代码。
本模块把其中「逐字/近乎逐字相同」的部分抽出为共用实现，两策略仅保留极薄的委托
包装（``async def _xxx(self, ...): return await protection_retry.yyy(...)``），
在消除重复的同时保证运行时行为零变化。

差异注入（禁止硬编码）：
- 策略实例以参数注入（提供 notification / positions / db_manager / 配置属性以及
  各类策略内下单助手）；共享函数统一通过 ``strategy.*`` 访问，从而既能复用同一套
  逻辑，又能让「实例级打桩」（如 ``s._notify_warning = AsyncMock()``）继续生效；
- 模块级可替换函数（``release_claim`` / ``close_remaining`` / ``cleanup_expired_claims``
  / ``try_claim_symbol`` / ``is_symbol_owned_by_other``）由策略侧在调用点传入「本策略
  模块作用域」的引用，从而让既有测试的 ``monkeypatch.setattr(strategy_module, ...)``
  继续生效；
- 通知项目名由策略侧以 ``self.strategy_name`` 注入，本模块不硬编码任何策略身份。

设计边界：本模块只承载「保护完整性 / 补挂收敛」「占用互斥辅助」「入场终态适配」，
各策略的差异化逻辑（如激进版的震荡入场三机制开关、策略 id / 名称）不在本模块内。

归属占用：占用表（position_claims）的互斥/占位/释放/清理原语统一由
``shared.position_ownership`` 提供，本模块只做「薄编排」，不重复实现占用逻辑。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import structlog

from .api_retry_config import is_order_fill_enabled
from .binance_api import BinanceAPIError
from .order_fill_waiter import OrderFillResult, wait_order_final_state

logger = structlog.get_logger()

# ============================================================
# §1 保护完整性纯逻辑（无 IO，两策略逐字相同）
# ============================================================


def protection_complete(position) -> bool:
    """三类保护单（硬止损 / TP1 / TP2）是否全部就位（保护完整性单点判定）"""
    return (
        position.stop_loss_order_id is not None
        and position.tp1_order_id is not None
        and position.tp2_order_id is not None
    )


def apply_protection_order_ids(position, order_ids: Dict[str, Optional[int]]) -> None:
    """把逐单保护下单结果写入持仓，并按完整性设置 protection_pending（R05）"""
    if order_ids.get('stop') is not None:
        position.stop_loss_order_id = order_ids['stop']
    if order_ids.get('tp1') is not None:
        position.tp1_order_id = order_ids['tp1']
    if order_ids.get('tp2') is not None:
        position.tp2_order_id = order_ids['tp2']
    position.protection_pending = not protection_complete(position)


def missing_protection_legs(position) -> List[str]:
    """列出当前缺失的保护腿（用于告警）"""
    legs = []
    if position.stop_loss_order_id is None:
        legs.append("STOP")
    if position.tp1_order_id is None:
        legs.append("TP1")
    if position.tp2_order_id is None:
        legs.append("TP2")
    return legs


def protection_due(position, cycle_count: int, interval_cycles: int) -> bool:
    """补挂节流：首次即时补挂，其后每 interval_cycles 个主循环尝试一次"""
    interval = max(interval_cycles, 1)
    if position.protection_retry_count == 0:
        return True
    return (cycle_count % interval) == 0


def mark_protection_pending_if_incomplete(position, symbol: str) -> None:
    """重启兜底：补挂后仍不完整 → 标记 protection_pending 交主循环收敛（R05）"""
    if protection_complete(position):
        return
    position.protection_pending = True
    logger.warning(
        f"{symbol} 启动恢复：保护单仍不完整，标记 protection_pending",
        missing=missing_protection_legs(position),
    )


# ============================================================
# §2 告警与占用互斥辅助（薄编排，占用原语复用 position_ownership）
# ============================================================


async def send_warning(notification_client, project: str, message: str) -> None:
    """统一告警推送（失败仅记日志，不中断主流程）"""
    try:
        await notification_client.send(message=message, level="warning", project=project)
    except Exception as e:
        logger.warning("发送告警失败", error=str(e))


async def handle_claim_conflict(strategy, symbol: str, owner: Optional[str]) -> None:
    """占用冲突处理：告警 + 跳过开仓（R07-F3，不抛异常中断主循环）"""
    logger.info(
        f"{symbol} 已被其他策略持有（占用互斥），跳过开仓",
        owner=owner,
        competing=strategy._competing_record_names,
    )
    await strategy._notify_warning(
        f"{symbol} 开仓被占用互斥拦截（已被 {owner or '其他策略'} 持有），跳过"
    )


async def release_claim_if_no_position(
    positions: Dict[str, Any], db_manager, symbol: str, my_record_name: str,
    reason: str, release_claim_fn,
) -> None:
    """仅在未登记持仓时释放占用（防保护失败/异常误放已建仓的占位，R07-F4）"""
    if symbol in positions:
        return
    await release_claim_fn(db_manager, symbol, my_record_name, reason=reason)


async def maybe_cleanup_expired_claims(strategy, cleanup_fn) -> None:
    """按 claim_cleanup_interval_minutes 定时清理过期占用（R07-F7）"""
    if not strategy._ownership_enabled:
        return
    now = datetime.now(timezone.utc)
    interval_minutes = max(strategy._claim_cleanup_interval_minutes, 1)
    if (strategy._last_claim_cleanup_time is not None
            and (now - strategy._last_claim_cleanup_time).total_seconds() < interval_minutes * 60):
        return
    strategy._last_claim_cleanup_time = now
    cleared = await cleanup_fn(strategy.db_manager)
    if cleared:
        logger.info("过期占用清理完成", cleared=cleared)


# ============================================================
# §3 入场执行：成交量解析 / 微仓清零 / 保护挂单 / 持仓构建与合并
# ============================================================


async def resolve_entry_quantity(strategy, symbol: str, entry) -> Optional[Decimal]:
    """按实际成交量解析可建仓数量（R05-F1 + R06-F5/D3）

    成交量经精度截断（向下取整到 stepSize）后：
    - > 0 → 返回该数量，用于建立持仓与保护单；
    - <= 0 → D3 微仓清零：调用 close_remaining 平掉残余成交，失败仅告警；
             返回 None，调用方放弃建仓（不登记持仓、释放占用）。
    """
    step_size, _tick_size = await strategy._get_precision_params(symbol)
    qty = strategy._adjust_quantity_precision(entry.executed_qty, step_size)
    if qty > 0:
        return qty
    logger.warning(
        f"{symbol} 入场成交量经精度截断后不足最小下单量，执行微仓清零（D3）",
        executed_qty=str(entry.executed_qty),
        step_size=step_size,
    )
    await strategy._zero_micro_entry(symbol, entry)
    return None


async def zero_micro_entry(strategy, symbol: str, entry, close_remaining_fn) -> None:
    """D3：残余微仓清零（走 shared.reduce_only_close.close_remaining）

    清零失败/异常仅告警，不抛异常中断主循环；占用由调用方释放。
    """
    try:
        outcome = await close_remaining_fn(strategy.binance, symbol, entry.executed_qty)
    except Exception as e:
        logger.error(f"{symbol} 微仓清零异常", error=str(e), exc_info=True)
        await strategy._notify_warning(f"{symbol} 微仓清零异常，请人工核查残余仓位")
        return
    if outcome.success:
        logger.info(f"{symbol} 微仓清零成功", closed_qty=str(outcome.closed_qty))
    else:
        logger.error(f"{symbol} 微仓清零失败", reason=outcome.reason)
        await strategy._notify_warning(
            f"{symbol} 微仓清零失败（{outcome.reason}），请人工核查残余仓位"
        )


async def fill_entry_protection_orders(
    strategy, symbol: str, signal: Dict, actual_quantity: Decimal
) -> Dict[str, Optional[int]]:
    """新开仓保护单：硬止损 + TP1/TP2（R05：逐单收集，失败不丢弃已成功 ID）

    全部读配置不硬编码；数量按**实际成交量**计算。任一保护单失败仅使对应键为 None，
    其余成功 ID 仍返回，交由调用方标记 protection_pending 后重试补挂。

    Returns:
        {'stop': id|None, 'tp1': id|None, 'tp2': id|None}
    """
    direction = signal['direction']
    stop_side = "SELL" if direction == "LONG" else "BUY"
    partial_cfg = strategy._get_grade_risk(signal.get('grade', 'A'))['partial_take_profit']
    stop_offset = Decimal(str(strategy.risk_config.get('stop_limit_order', {}).get('offset_pct', 0.002)))
    tp_offset = Decimal(str(strategy.risk_config.get('tp_limit_order', {}).get('offset_pct', 0.0015)))
    step_size, _tick_size = await strategy._get_precision_params(symbol)

    order_ids: Dict[str, Optional[int]] = {'stop': None, 'tp1': None, 'tp2': None}
    order_ids['stop'] = await _place_entry_stop_order(
        strategy, symbol, signal, actual_quantity, direction, stop_side, stop_offset, step_size)
    for level, ratio_key in ((1, 'tp1_close_ratio'), (2, 'tp2_close_ratio')):
        ratio = Decimal(str(partial_cfg[ratio_key]))
        order_ids[f'tp{level}'] = await _place_entry_tp_order(
            strategy, symbol, signal, actual_quantity, level, ratio,
            direction, stop_side, tp_offset, step_size)
    return order_ids


async def _place_entry_stop_order(
    strategy, symbol: str, signal: Dict, actual_quantity: Decimal,
    direction: str, stop_side: str, stop_offset: Decimal, step_size,
) -> Optional[int]:
    """入场硬止损条件单：全量、按实际成交量（R05 逐单收集，失败返回 None）"""
    initial_stop = Decimal(str(signal['initial_stop_loss']))
    stop_limit = strategy._apply_limit_offset(initial_stop, stop_offset, direction)
    stop_qty = strategy._adjust_quantity_precision(actual_quantity, step_size)
    logger.info(
        f"{symbol} 下止损限价单",
        stop_side=stop_side,
        stop_price=float(initial_stop),
        limit_price=float(stop_limit),
        quantity=float(stop_qty)
    )
    return await strategy._place_conditional_order_and_record(
        symbol, stop_side, "STOP", initial_stop, stop_limit,
        stop_qty, "STOP_LOSS", strategy.strategy_name
    )


async def _place_entry_tp_order(
    strategy, symbol: str, signal: Dict, actual_quantity: Decimal, level: int,
    ratio: Decimal, direction: str, stop_side: str, tp_offset: Decimal, step_size,
) -> Optional[int]:
    """入场单个止盈腿：按等级比例 × 实际成交量；数量精度截断为 0 则跳过（R05）"""
    tp_price = Decimal(str(signal[f'tp{level}_price']))
    tp_limit = strategy._apply_limit_offset(tp_price, tp_offset, direction)
    tp_qty = strategy._adjust_quantity_precision(actual_quantity * ratio, step_size)
    if tp_qty <= 0:
        logger.warning(
            f"{symbol} TP{level}止盈数量精度调整后为0，跳过下单（标记保护待补）",
            actual_quantity=float(actual_quantity),
            step_size=step_size,
        )
        return None
    return await strategy._place_tp_order(
        symbol, stop_side, tp_price, tp_limit, tp_qty, f"TP{level}", strategy.strategy_name
    )


def build_position_state(position_cls, signal: Dict, entry, actual_quantity: Decimal):
    """根据信号与成交结果构建新持仓状态（R05：用真实成交量/入场价）

    保护单 ID 尚未注入，故 protection_pending 初始置 True，由
    apply_protection_order_ids 依实际下单结果决定是否清除。
    """
    position = position_cls()
    # 入场价优先取成交均价（真实成交价），缺失则回退信号价
    avg_price = entry.avg_price if entry.avg_price and entry.avg_price > 0 else None
    position.entry_price = avg_price if avg_price is not None else Decimal(str(signal['entry_price']))
    position.entry_time = signal['timestamp']
    position.direction = signal['direction']
    position.initial_quantity = actual_quantity
    position.current_quantity = actual_quantity
    position.atr = Decimal(str(signal['atr']))
    position.grade = signal.get('grade', 'A')
    position.entry_order_id = entry.order_id
    position.protection_pending = True
    return position


def merge_position(position, signal: Dict, entry_order, actual_quantity: Decimal) -> None:
    """合并持仓（原地修改，v6.28）

    加权均价 = (旧entry×旧量 + 新entry×本次增量) / 合并后总量；更新
    initial/current_quantity 为交易所实际总量、ATR/grade 取新信号；保留
    highest/lowest、tp1_hit/tp2_hit、trailing、entry_time 等状态。
    """
    old_quantity = position.initial_quantity
    old_entry = position.entry_price or Decimal('0')
    new_entry = Decimal(str(signal['entry_price']))
    total_quantity = Decimal(str(actual_quantity))
    added_quantity = total_quantity - old_quantity

    # 加权均价：仅当确有增量成交时重算，避免除零
    if added_quantity > 0 and old_quantity > 0:
        position.entry_price = (old_entry * old_quantity + new_entry * added_quantity) / total_quantity
    elif added_quantity > 0:
        position.entry_price = new_entry

    position.initial_quantity = total_quantity
    position.current_quantity = total_quantity
    position.atr = Decimal(str(signal['atr']))
    position.grade = signal.get('grade', position.grade)
    position.entry_order_id = entry_order.order_id
    # highest_price/lowest_price 保留不重置；tp1_hit/tp2_hit 保持 False
    # trailing_stop_price/trailing_activated 保留（若已激活）；entry_time 保留最早入场时间


# ============================================================
# §4 保护补挂收敛（R05-F3/F4）
# ============================================================


async def replenish_protection_round(
    strategy, symbol: str, position, *, notify_first: bool = False
) -> bool:
    """保护补挂一轮：计数 → 尝试补挂 → 成功清 pending/计数，失败达上限触发 on_exhausted

    Returns:
        True = 三类保护单已全部就位（protection_pending 已清除）
    """
    if notify_first:
        await strategy._notify_protection_incomplete(symbol, position)
    position.protection_retry_count += 1
    if await strategy._replenish_missing_protection(symbol, position):
        position.protection_retry_count = 0
        return True
    if position.protection_retry_count >= strategy._protection_max_retries:
        await strategy._handle_protection_exhausted(symbol, position)
    return False


async def replenish_missing_protection(strategy, symbol: str, position) -> bool:
    """补挂缺失的保护腿（硬止损/TP1/TP2），复用统一下单助手（R05-F3）

    仅补挂当前缺失的腿；全部就位返回 True 并清除 protection_pending，
    仍缺失返回 False（保留 pending，交主循环节流收敛）。
    """
    try:
        (step_size, tick_size, stop_side, partial_cfg,
         stop_offset, tp_offset) = await strategy._load_order_rebuild_params(symbol, position)

        # 1. 硬止损（缺失才补）
        if position.stop_loss_order_id is None:
            stop_qty = strategy._adjust_quantity_precision(position.initial_quantity, step_size)
            if stop_qty > 0:
                stop_id = await strategy._place_stop_loss_order(
                    symbol, position, stop_qty, tick_size, stop_side, stop_offset)
                if stop_id:
                    position.stop_loss_order_id = stop_id

        # 2/3. TP1/TP2（缺失才补）
        for level, attr in ((1, 'tp1_order_id'), (2, 'tp2_order_id')):
            if getattr(position, attr) is not None:
                continue
            tp_id = await strategy._place_missing_tp_level(
                symbol, position, level, step_size, tick_size, stop_side, partial_cfg, tp_offset)
            if tp_id:
                setattr(position, attr, tp_id)

        position.protection_pending = not protection_complete(position)
        return not position.protection_pending
    except Exception as e:
        logger.error(f"{symbol} 补挂保护单异常", error=str(e), exc_info=True)
        return False


async def place_missing_tp_level(
    strategy, symbol: str, position, level: int, step_size, tick_size: Decimal,
    stop_side: str, partial_cfg: Dict, tp_offset: Decimal,
) -> Optional[str]:
    """补挂单个 TP 腿（R05 补挂；数量=initial×比例 精度截断，为0则跳过返回 None）"""
    tp_price = strategy._calculate_tp_price(
        position.entry_price, position.atr, position.direction, level, position.grade)
    tp_qty = strategy._adjust_quantity_precision(
        position.initial_quantity * Decimal(str(partial_cfg[f'tp{level}_close_ratio'])), step_size)
    if tp_qty <= 0:
        logger.warning(f"{symbol} TP{level}尾仓数量精度调整后为0，跳过补挂", level=level)
        return None
    tp_limit = strategy._adjust_price_precision(
        strategy._apply_limit_offset(tp_price, tp_offset, position.direction), tick_size)
    return await strategy._place_tp_order(
        symbol, stop_side, tp_price, tp_limit, tp_qty, None, strategy.strategy_name)


async def retry_protection_pending(strategy) -> None:
    """保护待补持仓的节流补挂（R05-F3/F4，update_positions 周期末调用）

    对 protection_pending=True 且 current_quantity>0 的持仓按 retry_interval_cycles
    节流补挂；成功清除 pending 与计数；累计达 max_retries 仍失败则按 on_exhausted
    处理（默认 alert，不减仓），此后不再尝试（保持 pending 供重启兜底）。
    """
    for symbol, position in list(strategy.positions.items()):
        if not position.protection_pending or position.current_quantity <= 0:
            continue
        if position.protection_retry_count >= strategy._protection_max_retries:
            continue
        if not strategy._protection_due(position):
            continue
        await strategy._replenish_protection_round(symbol, position)


async def handle_protection_exhausted(strategy, symbol: str, position) -> None:
    """保护补挂达上限处理（D2：默认 alert，不自动减仓）"""
    logger.error(
        f"{symbol} 保护单补挂达上限仍未完整",
        on_exhausted=strategy._protection_on_exhausted,
        retry_count=position.protection_retry_count,
        missing=strategy._missing_protection_legs(position),
    )
    if strategy._protection_on_exhausted == "alert" and strategy._protection_notify:
        await strategy._notify_protection_exhausted(symbol, position)


async def notify_protection_incomplete(strategy, symbol: str, position) -> None:
    """保护不完整告警（R05：开仓成功但保护待补）"""
    missing = '/'.join(strategy._missing_protection_legs(position))
    await strategy._notify_warning(
        f"{symbol} 持仓已建立但保护单不完整（缺失: {missing}），已进入补挂流程"
    )


async def notify_protection_exhausted(strategy, symbol: str, position) -> None:
    """保护补挂耗尽告警（D2：仅告警，不自动减仓，提醒人工关注裸仓风险）"""
    missing = '/'.join(strategy._missing_protection_legs(position))
    await strategy._notify_warning(
        f"{symbol} 保护单补挂达上限仍缺失（缺失: {missing}），已按 alert 处理，请人工关注裸仓风险"
    )


# ============================================================
# §5 入场订单终态适配（R06：统一走 order_fill_waiter）
# ============================================================


async def wait_for_order_fill(
    client, symbol: str, order_id: int, timeout_seconds: int, check_interval: float, *,
    legacy_fn,
) -> Optional[OrderFillResult]:
    """等待限价单至终态并返回结构化结果（R06）

    统一走 shared.order_fill_waiter.wait_order_final_state：超时撤单后重读最终成交量、
    -2013 可见延迟在等待循环内消化、识别部分成交（executedQty>0）。order_fill.enabled=false
    时降级为 D4 回退路径（legacy_fn），行为回到修复前。

    Returns:
        OrderFillResult；异常/持续不可见时返回 None（调用方按 has_fill 判定）
    """
    if not is_order_fill_enabled():
        return await legacy_fn(symbol, order_id, timeout_seconds, check_interval)
    try:
        return await wait_order_final_state(
            client, symbol, order_id=order_id, timeout_seconds=timeout_seconds,
        )
    except Exception as e:
        logger.error(f"{symbol} 等待订单终态异常", order_id=order_id, error=str(e))
        return None


async def wait_for_order_fill_legacy(
    client, symbol: str, order_id: int, timeout_seconds: int, check_interval: float, *,
    build_result_fn,
) -> Optional[OrderFillResult]:
    """D4 回退：order_fill.enabled=false 时的既有简易轮询（返回结构化结果统一调用方）

    行为与修复前一致（超时返回 None、CANCELED/EXPIRED/REJECTED 视为未成交）；
    仅供总闸关闭时使用，用于不改代码快速回退。
    """
    try:
        # PM API 订单可见性：下单后短暂延迟才可用，首循环前先小等
        await asyncio.sleep(min(check_interval, 0.5))
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        while datetime.now(timezone.utc) < deadline:
            try:
                order = await client.get_order(symbol, order_id)
            except BinanceAPIError as api_err:
                # [-2013] PM API 瞬时不可见，等待后重试
                if api_err.code == -2013:
                    await asyncio.sleep(check_interval)
                    continue
                raise
            if order.get("status", "") == "FILLED":
                return build_result_fn(order)
            if order.get("status", "") in ("CANCELED", "EXPIRED", "REJECTED"):
                return None
            await asyncio.sleep(check_interval)
        logger.warning(f"{symbol} 限价单超时未成交", order_id=order_id)
        return None
    except Exception as e:
        logger.error(f"{symbol} 检查限价单成交状态异常", order_id=order_id, error=str(e))
        return None


def build_legacy_fill_result(order: Dict[str, Any]) -> OrderFillResult:
    """由交易所订单对象构建 OrderFillResult（D4 回退路径，仅完全成交场景）"""
    executed = Decimal(str(order.get("executedQty") or 0))
    orig = Decimal(str(order.get("origQty") or 0))
    remaining = orig - executed
    if remaining < 0:
        remaining = Decimal("0")
    return OrderFillResult(
        status="FILLED",
        executed_qty=executed,
        orig_qty=orig,
        remaining_qty=remaining,
        avg_price=Decimal(str(order.get("avgPrice") or 0)),
        order_id=order.get("orderId"),
        client_order_id=order.get("clientOrderId"),
        raw=dict(order),
    )


async def place_and_wait_entry_order(strategy, symbol: str, signal: Dict) -> Optional[OrderFillResult]:
    """下限价单开仓并等待终态（v6.28 拆分子函数，R06 结构化等待）

    等待走 strategy._wait_for_order_fill（内部 wait_order_final_state）：超时由助手
    撤单并重读最终成交量，识别部分成交（executedQty>0）。

    Returns:
        有成交（完全/部分）返回 OrderFillResult；无成交返回 None（已撤单）
    """
    entry_side = "BUY" if signal['direction'] == "LONG" else "SELL"
    logger.info(
        f"{symbol} 下限价单开仓",
        side=entry_side,
        quantity=float(signal['quantity']),
        entry_price=float(signal['entry_price'])
    )
    entry_order = await strategy.binance.place_order(
        symbol=symbol,
        side=entry_side,
        quantity=signal['quantity'],
        price=signal['entry_price'],
        order_type="LIMIT"
    )
    entry_order_id = entry_order.get('orderId')
    logger.info(
        f"{symbol} 开仓订单已下单",
        order_id=entry_order_id,
        status=entry_order.get('status')
    )

    # 等待限价单至终态（超时时间从配置读取；撤单/重读由 wait_order_final_state 负责）
    entry_timeout = strategy.risk_config.get('position_sizing', {}).get('entry_order_timeout_seconds', 60)
    result = await strategy._wait_for_order_fill(symbol, entry_order_id, entry_timeout)
    if result is None or not result.has_fill:
        # 无成交（含超时撤单后确认未成交、未知终态）：与修复前一致，放弃开仓
        logger.warning(
            f"{symbol} 入场订单无成交，放弃开仓",
            order_id=entry_order_id,
            status=(result.status if result is not None else None),
        )
        return None
    return result


# ============================================================
# §6 新开仓主流程（R05/R06/R07 加固）
# ============================================================


async def open_new_position(strategy, signal: Dict, *, is_owned_fn, try_claim_fn) -> bool:
    """新开仓主流程（R05/R06/R07 加固）

    流程：归属互斥（R07-F5）→ 原子占位（R07）→ 频率记录 → 入场下单（R06 结构化终态
    等待，识别部分成交）→ 成交后立即登记持仓并按实际成交量挂保护单（R05）→ 保护不完整
    即时补挂一轮并交主循环节流收敛。任一保护单失败不得丢弃仓位（D2），缺口靠补挂收敛。

    Returns:
        是否执行成功（持仓是否建立；保护完整性由 protection_pending 表达）
    """
    symbol = signal['symbol']
    try:
        logger.info(f"执行交易信号: {symbol}", direction=signal['direction'],
                    grade=signal['grade'], score=signal['score'])

        if not await _acquire_entry_slot(
            strategy, symbol, is_owned_fn=is_owned_fn, try_claim_fn=try_claim_fn
        ):
            return False

        # 记录交易（频率控制）
        await strategy.frequency_controller.record_trade(symbol, signal['timestamp'])

        # 入场下单（设置杠杆 + 仓位检查 + 限价单 + 等待终态）
        entry = await strategy._place_entry_order(symbol, signal)
        if entry is None:
            await strategy._release_claim_if_no_position(symbol, reason="open_failed")
            return False

        # R05：成交确认后立即登记持仓（R06-F5：部分成交按实际成交量建仓，不丢弃）
        position = await _establish_position(strategy, symbol, signal, entry)
        if position is None:
            return False

        logger.info(
            f"交易信号执行完成: {symbol}",
            entry_order_id=position.entry_order_id,
            stop_loss_order_id=position.stop_loss_order_id,
            tp1_order_id=position.tp1_order_id,
            protection_pending=position.protection_pending,
        )
        return True

    except Exception as e:
        logger.error(f"执行交易信号失败: {symbol}", error=str(e), exc_info=True)
        await strategy._send_signal_error_notification(symbol, e)
        await strategy._release_claim_if_no_position(symbol, reason="open_exception")
        return False


async def _acquire_entry_slot(strategy, symbol: str, *, is_owned_fn, try_claim_fn) -> bool:
    """开仓前归属互斥（R07-F5）与原子占位（R07）：未取得占位返回 False（已告警）"""
    if await is_owned_fn(
        strategy.db_manager, symbol, strategy.my_record_name,
        strategy._competing_record_names, enabled=strategy._ownership_enabled,
    ):
        await strategy._handle_claim_conflict(symbol, None)
        return False
    claim = await try_claim_fn(
        strategy.db_manager, symbol, strategy.my_record_name,
        competing_record_names=strategy._competing_record_names,
        ttl_minutes=strategy._claim_ttl_minutes,
        enabled=strategy._ownership_enabled,
        lock_timeout_seconds=strategy._lock_timeout_seconds,
    )
    if not claim.get('claimed'):
        await strategy._handle_claim_conflict(symbol, claim.get('owner'))
        return False
    return True


async def _establish_position(strategy, symbol: str, signal: Dict, entry):
    """成交后登记持仓并挂保护单（R05/R06），返回持仓；微仓清零返回 None"""
    actual_qty = await strategy._resolve_entry_quantity(symbol, entry)
    if actual_qty is None:
        await strategy._release_claim_if_no_position(symbol, reason="micro_zeroed")
        return None
    position = strategy._build_position_state(signal, entry, actual_qty)
    strategy.positions[symbol] = position
    # 下硬止损/TP1/TP2 保护单（失败保留已成功 ID，不丢仓位）
    order_ids = await strategy._place_entry_protection_orders(symbol, signal, actual_qty)
    strategy._apply_protection_order_ids(position, order_ids)
    if position.protection_pending:
        # 即时补挂一轮（R05-F3）；仍不完整则保留 pending 交由主循环收敛
        await strategy._replenish_protection_round(symbol, position, notify_first=True)
    return position