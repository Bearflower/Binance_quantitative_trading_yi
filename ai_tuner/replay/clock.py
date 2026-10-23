"""确定性事件时钟。

事件时间只来自快照内容（K 线 close_time / 资金费结算时间 / 快照 bucket_end），
不读系统时钟、不使用随机数、不依赖 dict/set 的遍历顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

BAR_CLOSE = "bar_close"
FUNDING = "funding_settlement"
SNAPSHOT = "external_snapshot"

# 同一时刻的事件处理次序（数字小者先）：资金费先计提，再喂外部快照，最后推进 K 线。
EVENT_PRIORITY: Dict[str, int] = {
    FUNDING: 0,
    SNAPSHOT: 1,
    BAR_CLOSE: 2,
}


@dataclass(frozen=True)
class Event:
    """一个确定性事件。seq 由 build_schedule 统一分配，保证全序。"""

    time: datetime
    kind: str
    subject: str
    seq: int
    payload: Any


def sort_key(event: Event) -> Tuple[datetime, int, str, int]:
    """全序键：时间 → 事件类别优先级 → 主体名 → seq。

    主体名参与排序是为了在「同一毫秒同一类别」时也不依赖输入顺序。
    """
    return (
        event.time,
        EVENT_PRIORITY.get(event.kind, 99),
        event.subject,
        event.seq,
    )


def build_schedule(events: Iterable[Event]) -> Tuple[Event, ...]:
    """按全序键排序并重排 seq（0,1,2,...）。

    输入 Event 的 seq 只作为同键内的稳定次序（调用方按快照顺序生成），
    输出 seq 是最终确定性序号，写入 audit_events 后逐字节可复现。
    """
    ordered = sorted(events, key=sort_key)
    return tuple(
        Event(time=e.time, kind=e.kind, subject=e.subject, seq=index, payload=e.payload)
        for index, e in enumerate(ordered)
    )


def collect_bar_events(bars_by_subject: Dict[str, List[Any]]) -> List[Event]:
    """把 bars 映射转成 bar_close 事件（键为 ``symbol|interval``，已按快照顺序）。"""
    events: List[Event] = []
    seq = 0
    for subject in sorted(bars_by_subject):
        for bar in bars_by_subject[subject]:
            events.append(
                Event(time=bar.close_time, kind=BAR_CLOSE, subject=subject, seq=seq, payload=bar)
            )
            seq += 1
    return events


def collect_funding_events(funding_by_symbol: Dict[str, List[Tuple[datetime, Any]]]) -> List[Event]:
    """把资金费结算点转成 funding 事件。"""
    events: List[Event] = []
    seq = 0
    for symbol in sorted(funding_by_symbol):
        for funding_time, rate in funding_by_symbol[symbol]:
            events.append(
                Event(
                    time=funding_time,
                    kind=FUNDING,
                    subject=symbol,
                    seq=seq,
                    payload=rate,
                )
            )
            seq += 1
    return events


def collect_snapshot_events(snapshots: Iterable[Any]) -> List[Event]:
    """把外部时点快照转成 snapshot 事件（以 bucket_end 作为事件时间）。"""
    events: List[Event] = []
    for index, snap in enumerate(snapshots):
        events.append(
            Event(
                time=snap.bucket_start,
                kind=SNAPSHOT,
                subject=snap.symbol,
                seq=index,
                payload=snap,
            )
        )
    return events
