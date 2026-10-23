"""共享事件循环。

推进顺序（契约 §1.1 / §6）：
1. ``adapter.prepare`` 校验必需数据（缺 → DataBlocked）；
2. 由快照生成确定性事件序列（资金费 → 外部快照 → K 线收盘）；
3. 逐事件推进：Broker 撮合 → 记录盯市权益 → 适配器更新状态 → 产出意图 → Broker 排队；
4. 汇总 ReplayResult 与 audit trail（按 (ts, seq) 固定）。

本模块不 import ``backtest.*``，不重算策略评分（评分由适配器提供）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .broker import VirtualBroker
from .clock import (
    BAR_CLOSE,
    FUNDING,
    SNAPSHOT,
    Event,
    build_schedule,
    collect_bar_events,
    collect_funding_events,
    collect_snapshot_events,
)
from .data_portal import DataPortal
from .models import DataBlocked, EquityPoint, ReplayResult, to_utc_z


def run_replay(
    portal: DataPortal,
    adapter: Any,
    broker: VirtualBroker,
    include_snapshots: bool = False,
) -> ReplayResult:
    """执行一次确定性回放。

    Args:
        portal: 已校验 manifest 的只读门户。
        adapter: ``ReplayAdapter`` 实现（prepare/on_bar/on_snapshot/intents）。
        broker: 统一虚拟 Broker（含成本档参数）。
        include_snapshots: 是否把外部时点快照纳入事件流（HRS=True）。

    Returns:
        ReplayResult（equity_curve / closed_trades / audit_events / violations）。
    """
    ctx = adapter.prepare(portal)

    bars_by_subject: Dict[str, List[Any]] = {}
    for symbol in ctx.universe.symbols:
        for interval in ctx.universe.intervals:
            bars = portal.bars(symbol, interval)
            if not bars:
                raise DataBlocked(["kline_empty:{0}:{1}".format(symbol, interval)])
            bars_by_subject["{0}|{1}".format(symbol, interval)] = list(bars)

    funding_by_symbol: Dict[str, List[Tuple[datetime, Decimal]]] = {}
    for symbol in ctx.universe.symbols:
        funding_by_symbol[symbol] = list(
            portal.funding_rates(symbol, portal.window_start, portal.as_of)
        )

    snapshots: List[Any] = []
    if include_snapshots and getattr(adapter, "requires_external_snapshot", False):
        for symbol in ctx.universe.symbols:
            snapshots.extend(portal.external_snapshot(symbol))

    events: List[Event] = []
    events.extend(collect_bar_events(bars_by_subject))
    events.extend(collect_funding_events(funding_by_symbol))
    if snapshots:
        events.extend(collect_snapshot_events(snapshots))
    schedule = build_schedule(events)

    audit: List[Dict[str, Any]] = [
        {
            # 内部 audit 统一持 datetime，仅在 _finalize_audit 转 UTC Z 字符串
            "ts": portal.window_start,
            "type": "run_start",
            "payload": {
                "strategy_id": ctx.strategy_id,
                "as_of": to_utc_z(portal.as_of),
                "manifest_sha256": portal.manifest_sha256,
                "window_start": to_utc_z(portal.window_start),
                "window_end": to_utc_z(portal.window_end),
            },
        }
    ]
    equity: Dict[datetime, EquityPoint] = {}

    for event in schedule:
        if event.kind == FUNDING:
            broker.settle_funding(event.subject, event.payload, event.time)
        elif event.kind == SNAPSHOT:
            ctx.portfolio = broker.view()
            adapter.on_snapshot(event.payload, ctx)
        elif event.kind == BAR_CLOSE:
            bar = event.payload
            broker.on_bar(bar.symbol, bar, bar.close_time)
            if bar.close_time not in equity:
                equity[bar.close_time] = broker.mark(bar.close_time)
            ctx.portfolio = broker.view()
            adapter.on_bar(bar, ctx)
            intents = adapter.intents(ctx)
            if intents:
                broker.submit(intents, bar, bar.close_time)
            _apply_protective_prices(broker, adapter, bar.symbol)
        _drain_broker_audit(broker, audit, event.time)

    if portal.as_of not in equity:
        equity[portal.as_of] = broker.mark(portal.as_of)

    return ReplayResult(
        manifest_ref=portal.manifest_sha256,
        equity_curve=tuple(equity[key] for key in sorted(equity)),
        closed_trades=tuple(broker.closed_trades),
        final_position=broker.final_position(),
        audit_events=tuple(_finalize_audit(audit)),
        invariant_violations=tuple(broker.invariant_violations),
    )


def _apply_protective_prices(broker: VirtualBroker, adapter: Any, symbol: str) -> None:
    """持仓存在时，把适配器算出的止损/止盈价格写入持仓（Broker 不重算）。"""
    position = broker.positions.get(symbol)
    if position is None or not position.is_open():
        return
    protective = adapter.protective_prices(symbol)
    if protective:
        broker.set_protective_prices(symbol, **protective)


def _drain_broker_audit(
    broker: VirtualBroker, audit: List[Dict[str, Any]], event_time: Optional[datetime]
) -> None:
    """把 Broker 本轮产生的事件并入总 audit（补齐缺失时间戳）。"""
    if not broker.audit:
        return
    for item in broker.audit:
        if item.get("ts") is None:
            item["ts"] = event_time
    audit.extend(broker.audit)
    broker.audit = []


def _finalize_audit(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """保留发生顺序为 seq，并按 (ts, seq) 输出固定行序。"""
    prepared = [
        {
            "ts": to_utc_z(event["ts"]) if event.get("ts") else "",
            "seq": index,
            "type": event.get("type"),
            "payload": event.get("payload", {}),
        }
        for index, event in enumerate(events)
    ]
    prepared.sort(key=lambda item: (item["ts"], item["seq"]))
    return [
        {
            "ts": item["ts"],
            "seq": position,
            "type": item["type"],
            "payload": item["payload"],
        }
        for position, item in enumerate(prepared)
    ]
