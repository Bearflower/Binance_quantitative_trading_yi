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
    CASH_FLOW,
    DECISION,
    DAILY_MARK,
    FUNDING,
    FUNDING_DUE,
    FUNDING_RECEIPT,
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
from .data_portal import DataPortal
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
        funding_schedule: 逐币种预期结算时间。HRS 必须匹配 manifest 内独立的
            前瞻资金费日程观测；其他适配器保留显式合成夹具接口。
        funding_schedule_source: 合成测试必须显式传 synthetic_fixture；单独改标签
            不能把真实历史日程升级为已证明。

    Returns:
        ReplayResult（equity_curve / closed_trades / audit_events / violations）。
    """
    ctx = adapter.prepare(portal)

    if hasattr(adapter, 'configure_execution'):
        adapter.configure_execution(ctx)
    if hasattr(adapter, 'initial_positions'):
        broker.restore_initial_positions(adapter.initial_positions(ctx))
        ctx.portfolio = broker.view()
        broker.submit(adapter.initial_orders(ctx), None, portal.window_start)
        adapter.initialize_portfolio(ctx)
    execution_interval = getattr(adapter, 'execution_interval', None)
    batch_bars = getattr(adapter, 'batch_bar_updates', False)

    bars_by_subject: Dict[str, List[Any]] = {}
    for symbol in ctx.universe.symbols:
        for interval in tuple(dict.fromkeys((*ctx.universe.intervals, *getattr(adapter, "extra_intervals", ())))):
            bars = portal.bars(symbol, interval)
            if not bars:
                raise DataBlocked(["kline_empty:{0}:{1}".format(symbol, interval)])
            bars_by_subject["{0}|{1}".format(symbol, interval)] = [
                bar for bar in bars if bar.close_time > portal.window_start
            ]

    funding_by_symbol: Dict[str, List[Tuple[datetime, Decimal]]] = {}
    for symbol in ctx.universe.symbols:
        funding_by_symbol[symbol] = list(
            portal.funding_rates(symbol, portal.window_start, portal.as_of)
        )

    if getattr(adapter, 'requires_attested_funding', False):
        from .funding_history import attested_funding_schedule
        attested = attested_funding_schedule(portal.funding_schedule_evidence(),
            ctx.universe.symbols, portal.window_start, portal.as_of)
        if funding_schedule is not None and {k:tuple(v) for k,v in funding_schedule.items()} != attested:
            raise DataBlocked(['funding_schedule_evidence_mismatch'])
        funding_schedule = attested
        funding_schedule_source = 'prospective_exchange_forecast'
    if funding_schedule is not None and not getattr(adapter, 'requires_attested_funding', False) and (
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
               t.utcoffset() is None or not (portal.window_start < t <= portal.as_of)
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
                if portal.window_start < max(snap.observed_at, snap.bucket_end or snap.bucket_start) <= portal.as_of
            )

    events: List[Event] = []
    if hasattr(adapter,'cash_flow_events'):
        for row in adapter.cash_flow_events(ctx):
            events.append(Event(parse_utc(row['occurred_at']),CASH_FLOW,row['event_id'],len(events),row))
    if hasattr(adapter,'decision_events'):
        for row in adapter.decision_events(ctx):
            events.append(Event(parse_utc(row['available_at']),DECISION,row['symbol'],len(events),row))
    events.extend(collect_bar_events(bars_by_subject))
    delayed_funding=getattr(adapter,'requires_attested_funding',False)
    if delayed_funding:
        for symbol,rows in funding_by_symbol.items():
            for economic_time,rate in rows:
                events.append(Event(economic_time,FUNDING_DUE,symbol,len(events),economic_time))
                received_at=max(economic_time,portal.funding_receipt_time(symbol,economic_time))
                if received_at<=portal.as_of:
                    events.append(Event(received_at,FUNDING_RECEIPT,symbol,len(events),(economic_time,rate)))
    else:
        events.extend(collect_funding_events(funding_by_symbol))
    if snapshots:
        events.extend(collect_snapshot_events(snapshots))
    events.extend(collect_daily_mark_events(portal.window_start, portal.as_of))
    events.extend(collect_funding_check_events(expected, portal.window_start, portal.as_of))
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
        simultaneous_events = tuple(simultaneous_events)
        funding_received: set[str] = set()
        if batch_bars:
            # All trend bars ending now are visible before any same-hour scoring.
            for pending_event in simultaneous_events:
                if pending_event.kind == BAR_CLOSE:
                    ctx.portfolio = broker.view()
                    adapter.on_bar(pending_event.payload, ctx)
                    if pending_event.payload.interval == execution_interval:
                        broker.set_mark_price(pending_event.payload.symbol,pending_event.payload.close,pending_event.payload.close_time)
        for event in simultaneous_events:
            if event.kind == FUNDING_DUE:
                key=broker.begin_funding(event.subject,event.time)
                if key is not None:
                    adapter.on_funding_due(key,event.time)
                funding_received.add(event.subject)
            elif event.kind == FUNDING_RECEIPT:
                economic_time,rate=event.payload
                key=event.subject+'|'+economic_time.isoformat()
                if adapter.funding_is_pending(key):
                    try:
                        mark=portal.funding_mark_at(event.subject,economic_time,as_of=event.time)
                    except DataBlocked as exc:
                        adapter.on_funding_unproven(key,exc)
                    else:
                        payload=broker.receive_funding(key,rate,event.time,mark)
                        broker.set_mark_price(event.subject,mark['price'],mark['marked_at'])
                        adapter.on_funding_received(key,payload,mark,event.time)
            elif event.kind == FUNDING:
                broker.settle_funding(event.subject,event.payload,event.time)
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
            elif event.kind == CASH_FLOW:
                ctx.portfolio=broker.view()
                adapter.on_cash_flow(event.payload,ctx)
                broker.apply_cash_flow(Decimal(str(event.payload['amount'])),event.time,event.payload['event_id'])
            elif event.kind == SNAPSHOT:
                ctx.portfolio = broker.view()
                adapter.on_snapshot(event.payload, ctx)
            elif event.kind == BAR_CLOSE:
                bar = event.payload
                if execution_interval is None or bar.interval == execution_interval:
                    if batch_bars:
                        def after_entries():
                            ctx.portfolio = broker.view()
                            position = broker.positions.get(bar.symbol)
                            if position is not None and position.is_open():
                                brackets = adapter.on_entry_fill(position, ctx)
                                if brackets:
                                    broker.submit(brackets, bar, bar.close_time)
                                _apply_protective_prices(broker, adapter, bar.symbol)
                        broker.on_bar(bar.symbol, bar, bar.close_time,
                            after_entries=after_entries,
                            entry_ts=bar.close_time if execution_interval == '1m' else bar.open_time)
                    else:
                        broker.on_bar(bar.symbol, bar, bar.close_time)
                    if hasattr(adapter,'on_execution_bar'):
                        ctx.portfolio=broker.view()
                        maintenance=adapter.on_execution_bar(bar,ctx)
                        if maintenance:
                            broker.submit(maintenance,bar,bar.close_time)
                        _apply_protective_prices(broker,adapter,bar.symbol)
                    last_bar_close[bar.symbol] = bar.close_time
                ctx.portfolio = broker.view()
                if not batch_bars:
                    adapter.on_bar(bar, ctx)
                    intents = adapter.intents(ctx)
                    if intents:
                        broker.submit(intents, bar, bar.close_time)
                    _apply_protective_prices(broker, adapter, bar.symbol)
                _require_funding_schedule(broker, expected)
            elif event.kind == DECISION:
                ctx.portfolio=broker.view()
                adapter.on_decision(event.payload,ctx)
                intents=adapter.intents(ctx)
                if intents:
                    latest=next((candidate for candidate in reversed(bars_by_subject.get(event.subject+'|'+execution_interval,[]))
                                 if candidate.close_time<=event.time),None)
                    if latest is None:
                        raise DataBlocked(['hrs_decision_execution_bar_missing:'+event.subject])
                    broker.submit(intents,latest,event.time)
                _require_funding_schedule(broker,expected)
                _apply_protective_prices(broker,adapter,event.subject)
            elif event.kind == DAILY_MARK:
                missing = [
                    "daily_equity_gap:{0}:{1}".format(symbol, to_utc_z(timestamp))
                    for symbol in sorted(broker.positions)
                    if broker.positions[symbol].is_open()
                    and last_bar_close.get(symbol) != timestamp
                ]
                if missing:
                    raise DataBlocked(missing)
            ctx.portfolio=broker.view()
            if hasattr(adapter,'on_execution_events'):
                adapter.on_execution_events(tuple(broker.audit),ctx,at=event.time)
            _drain_broker_audit(broker, audit, event.time)
            if getattr(adapter, 'audit', None):
                audit.extend(adapter.audit)
                adapter.audit = []
        if batch_bars and not getattr(adapter,'requires_decision_calendar',False):
            # Legacy batch adapters retain their explicit bar-driven contract.
            for decision_event in simultaneous_events:
                if decision_event.kind != BAR_CLOSE or decision_event.payload.interval != '1h':
                    continue
                bar=decision_event.payload
                ctx.portfolio=broker.view()
                adapter.on_bar(bar,ctx)
                intents=adapter.intents(ctx)
                if intents:
                    broker.submit(intents,bar,bar.close_time)
                ctx.portfolio=broker.view()
                adapter.on_execution_events(tuple(broker.audit),ctx,at=timestamp)
                _require_funding_schedule(broker,expected)
                _apply_protective_prices(broker,adapter,bar.symbol)
                _drain_broker_audit(broker,audit,timestamp)
                audit.extend(adapter.audit)
                adapter.audit=[]
        ctx.portfolio = broker.view()
        qualified=True
        if hasattr(adapter, 'on_portfolio_mark'):
            qualified=adapter.on_portfolio_mark(timestamp, ctx) is not False
        if qualified:
            equity[timestamp] = broker.mark(timestamp)
        if hasattr(adapter,'reconciled_equity_points'):
            equity.update({point.ts:point for point in adapter.reconciled_equity_points()})
        if getattr(adapter,'audit',None):
            audit.extend(adapter.audit)
            adapter.audit=[]

    if hasattr(adapter,'finalize_funding'):
        adapter.finalize_funding()
    equity[portal.as_of] = broker.mark(portal.as_of)

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
