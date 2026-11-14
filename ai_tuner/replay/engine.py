"""共享事件循环。

推进顺序（契约 §1.1 / §6）：
1. ``adapter.prepare`` 校验必需数据（缺 → DataBlocked）；
2. 由快照生成确定性事件序列（资金费 → 外部快照 → K 线收盘）；
3. 逐事件推进 Broker 与适配器；同一时刻全部事件完成后记录一次盯市权益；
4. 汇总 ReplayResult 与 audit trail（按 (ts, seq) 固定）。

本模块不 import ``backtest.*``，不重算策略评分（评分由适配器提供）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from itertools import groupby
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .broker import VirtualBroker
from .clock import (
    BAR_CLOSE,
    DAILY_MARK,
    FUNDING,
    FUNDING_CHECK,
    SNAPSHOT,
    Event,
    build_schedule,
    collect_bar_events,
    collect_daily_mark_events,
    collect_funding_check_events,
    collect_funding_events,
    collect_snapshot_events,
)
from .data_portal import DataPortal, INTERVAL_MINUTES
from .models import DataBlocked, EquityPoint, ReplayResult, parse_utc, to_utc_z


def run_replay(
    portal: DataPortal,
    adapter: Any,
    broker: VirtualBroker,
    include_snapshots: bool = False,
    funding_schedule: Optional[Mapping[str, Sequence[datetime]]] = None,
    funding_schedule_source: Optional[str] = None,
) -> ReplayResult:
    """执行一次确定性回放。

    Args:
        portal: 已校验 manifest 的只读门户。
        adapter: ``ReplayAdapter`` 实现（prepare/on_bar/on_snapshot/intents）。
        broker: 统一虚拟 Broker（含成本档参数）。
        include_snapshots: 是否把外部时点快照纳入事件流（HRS=True）。
        funding_schedule: 逐币种预期结算时间。当前只接受合成夹具；真实历史
            日程尚无独立来源证明，因此真实策略保持 BLOCKED_DATA。
        funding_schedule_source: 合成测试必须显式传 synthetic_fixture。

    Returns:
        ReplayResult（equity_curve / closed_trades / audit_events / violations）。
    """
    ctx = adapter.prepare(portal)

    bars_by_subject: Dict[str, List[Any]] = {}
    for symbol in ctx.universe.symbols:
        for interval in ctx.universe.intervals:
            bars = [bar for bar in portal.bars(symbol, interval)
                    if portal.window_start < bar.close_time <= portal.window_end]
            if not bars:
                raise DataBlocked(["kline_empty:{0}:{1}".format(symbol, interval)])
            bars_by_subject["{0}|{1}".format(symbol, interval)] = bars

    if not bars_by_subject:
        raise DataBlocked(["window_has_no_market_stream"])
    if any(interval not in INTERVAL_MINUTES for interval in ctx.universe.intervals):
        raise DataBlocked(["unknown_execution_interval"])
    execution_interval = min(ctx.universe.intervals, key=INTERVAL_MINUTES.__getitem__)
    funding_by_symbol: Dict[str, List[Tuple[datetime, Decimal]]] = {}
    for symbol in ctx.universe.symbols:
        funding_by_symbol[symbol] = list(
            portal.funding_rates(symbol, portal.window_start, portal.window_end)
        )

    if funding_schedule is not None and (
        ctx.strategy_id != "fixture" or funding_schedule_source != "synthetic_fixture"
    ):
        raise DataBlocked(["historical_funding_schedule_unattested"])
    expected = funding_schedule or {}
    for symbol, times in expected.items():
        if symbol not in ctx.universe.symbols:
            raise DataBlocked(["funding_schedule_unknown_symbol:{0}".format(symbol)])
        if not isinstance(times, (list, tuple)) or len(times) > 24 * 90:
            raise DataBlocked(["funding_schedule_size_invalid:{0}".format(symbol)])
        if any(not isinstance(t, datetime) or t.tzinfo is None or
               t.utcoffset() is None or not (portal.window_start < t <= portal.window_end)
               for t in times):
            raise DataBlocked(["funding_schedule_time_invalid:{0}".format(symbol)])
        if len(times) != len(set(times)):
            raise DataBlocked(["funding_schedule_duplicate_time:{0}".format(symbol)])
        observed = [time for time, _ in funding_by_symbol[symbol]]
        if len(observed) != len(set(observed)):
            raise DataBlocked(["funding_duplicate_time:{0}".format(symbol)])
        missing = sorted(set(times) - set(observed))
        unexpected = sorted(set(observed) - set(times))
        if missing:
            raise DataBlocked(["funding_gap:{0}:{1}".format(symbol, to_utc_z(missing[0]))])
        if unexpected:
            raise DataBlocked(["funding_unexpected:{0}:{1}".format(
                symbol, to_utc_z(unexpected[0]))])
    _require_funding_schedule(broker, expected)

    snapshots: List[Any] = []
    if include_snapshots and getattr(adapter, "requires_external_snapshot", False):
        for symbol in ctx.universe.symbols:
            snapshots.extend(
                snap for snap in portal.external_snapshot(symbol)
                if portal.window_start < snap.available_at <= portal.window_end
            )

    events: List[Event] = []
    events.extend(collect_bar_events(bars_by_subject))
    events.extend(collect_funding_events(funding_by_symbol))
    if snapshots:
        events.extend(collect_snapshot_events(snapshots))
    events.extend(collect_daily_mark_events(portal.window_start, portal.window_end))
    events.extend(collect_funding_check_events(expected, portal.window_start, portal.window_end))
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
    equity: Dict[datetime, EquityPoint] = {
        portal.window_start: EquityPoint(
            portal.window_start, broker.initial_equity, broker.initial_equity, False
        )
    }
    last_bar_close: Dict[str, datetime] = {}

    for timestamp, simultaneous_events in groupby(schedule, key=lambda event: event.time):
        funding_received: set[str] = set()
        for event in simultaneous_events:
            if event.kind == FUNDING:
                broker.settle_funding(event.subject, event.payload, event.time)
                funding_received.add(event.subject)
            elif event.kind == FUNDING_CHECK:
                missing = [
                    "funding_gap:{0}:{1}".format(event.subject, to_utc_z(timestamp))
                    for position in (broker.positions.get(event.subject),)
                    if position is not None and position.is_open()
                    and position.entry_time < timestamp
                    and event.subject not in funding_received
                ]
                if missing:
                    raise DataBlocked(missing)
            elif event.kind == SNAPSHOT:
                ctx.portfolio = broker.view()
                adapter.on_snapshot(event.payload, ctx)
            elif event.kind == BAR_CLOSE:
                bar = event.payload
                if bar.interval == execution_interval:
                    broker.on_bar(bar.symbol, bar, bar.close_time)
                    last_bar_close[bar.symbol] = bar.close_time
                ctx.portfolio = broker.view()
                adapter.on_bar(bar, ctx)
                intents = adapter.intents(ctx)
                if intents:
                    broker.submit(intents, bar, bar.close_time)
                _require_funding_schedule(broker, expected)
                _apply_protective_prices(broker, adapter, bar.symbol)
            elif event.kind == DAILY_MARK:
                missing = [
                    "daily_equity_gap:{0}:{1}".format(symbol, to_utc_z(timestamp))
                    for symbol in sorted(broker.positions)
                    if broker.positions[symbol].is_open()
                    and last_bar_close.get(symbol) != timestamp
                ]
                if missing:
                    raise DataBlocked(missing)
            _drain_broker_audit(broker, audit, event.time)
        equity[timestamp] = broker.mark(timestamp)

    equity[portal.window_end] = broker.mark(portal.window_end)

    return ReplayResult(
        manifest_ref=portal.manifest_sha256,
        equity_curve=tuple(equity[key] for key in sorted(equity)),
        closed_trades=tuple(broker.closed_trades),
        final_position=broker.final_position(),
        audit_events=tuple(_finalize_audit(audit)),
        invariant_violations=tuple(broker.invariant_violations),
    )


def _require_funding_schedule(
    broker: VirtualBroker, expected: Mapping[str, Sequence[datetime]],
) -> None:
    missing = ["funding_schedule_unattested:{0}".format(symbol)
               for symbol in sorted(broker.positions)
               if broker.positions[symbol].is_open() and symbol not in expected]
    if missing:
        raise DataBlocked(missing)


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
            "ts": event.get("ts"),
            "seq": index,
            "type": event.get("type"),
            "payload": event.get("payload", {}),
        }
        for index, event in enumerate(events)
    ]
    prepared.sort(key=lambda item: (
        parse_utc(item["ts"]) if item["ts"] is not None else datetime.min.replace(tzinfo=timezone.utc),
        item["seq"],
    ))
    return [
        {
            "ts": to_utc_z(item["ts"]) if item["ts"] is not None else "",
            "seq": position,
            "type": item["type"],
            "payload": item["payload"],
        }
        for position, item in enumerate(prepared)
    ]
