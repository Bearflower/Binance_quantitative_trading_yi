"""状态层：分级状态机、持续计时、episode/reference 去重、恢复与快照接收（纯逻辑）。

口径权威：需求 §4.2/§7、计划 §2.3。
- 每方向独立：holds 用 setdefault/pop 维护，t-t0>=hold 才触发，中断/参考切换清零。
- 升级绕过冷却；同级按间隔重复；降级不发事件；临界分支每 episode 一次，其后持续
  恶化可追加一次原因升级。
- 终止价事实锁存（VALID/STALE 均可报，STALE 加标注）。
- AC-02：仅 SENT 且合法的快照更新参考；失败/UNKNOWN/冷却跳过沿用旧版。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from . import rules
from .features import FeatureSlice, MS_PER_SECOND
from .rules import (DOWN, DIRECTIONS, Level, ReferenceSnapshot, Region,
                    classify_region, grid_distance, validate_snapshot)

VALID, STALE = "VALID", "STALE"
MISSING, INVALID = "MISSING", "INVALID"

NORMAL = "normal"
INSIDE_URGENT = "inside_urgent"
BUFFER_URGENT = "buffer_urgent"
CRITICAL = "critical"
BOUNDARY = "boundary"
_HOLD_BRANCHES = (NORMAL, INSIDE_URGENT, BUFFER_URGENT)
_URGENT_BRANCHES = (INSIDE_URGENT, BUFFER_URGENT)


@dataclass(frozen=True)
class AlertEvent:
    """分级事件（event_id 确定性生成：参考+方向+episode+级别+原因）。"""

    event_id: str
    episode_id: str
    reference_id: str
    direction: str
    level: str
    reason: str
    decision_ms: int
    price: Decimal
    first_held_ms: Optional[int] = None
    gap_crossing: bool = False
    stale: bool = False
    reference_switch: bool = False


@dataclass
class DirectionRuntime:
    """单方向运行时状态。"""

    direction: str
    episode_serial: int = 0
    episode_id: Optional[str] = None
    holds: Dict[str, Optional[int]] = field(default_factory=dict)
    current_level: Level = Level.IDLE
    highest_level: Level = Level.IDLE
    critical_sent: bool = False
    sustained_after_critical: bool = False
    boundary_fact: bool = False
    last_urgent_ms: Optional[int] = None
    last_notice_ms: Optional[int] = None
    last_boundary_ms: Optional[int] = None
    recovery_t0: Optional[int] = None
    entries: List[int] = field(default_factory=list)


class RealtimeEngine:
    """实时/回放共用引擎；输入为整秒特征与逐笔事实，输出事件序列。"""

    def __init__(self, cfg: Dict, snapshot: Optional[ReferenceSnapshot] = None):
        self.cfg = cfg
        self.snapshot = snapshot
        self.invalid = False
        self.runtimes: Dict[str, DirectionRuntime] = {
            d: DirectionRuntime(d) for d in DIRECTIONS}
        self.events: List[AlertEvent] = []

    # ───────────────────── 参考与状态 ─────────────────────

    def reference_status(self, now_ms: int) -> str:
        """按 effective_at+max_age 派生 VALID/STALE；无参考 MISSING/INVALID。"""
        if self.snapshot is None:
            return INVALID if self.invalid else MISSING
        age = now_ms - self.snapshot.effective_at_ms
        if age > self.cfg["reference"]["max_age_seconds"] * MS_PER_SECOND:
            return STALE
        return VALID

    def apply_send_result(self, outcome: str, candidate: ReferenceSnapshot,
                         now_ms: int,
                         current_price: Optional[Decimal] = None) -> List[AlertEvent]:
        """小时出口发送结果（AC-02）。

        SENT+合法 → 切换参考（同 reference_id 不重复处理）；SENT+非法 → INVALID；
        FAILED/UNKNOWN/冷却跳过/计算失败 → 沿用当前参考，不索要确认。
        """
        if outcome != "SENT":
            return []
        try:
            validate_snapshot(candidate, self.cfg["symbol"])
        except ValueError:
            self._invalidate_reference()
            return []
        if self.snapshot is not None and candidate.reference_id == self.snapshot.reference_id:
            return []
        events = self._switch_reference(candidate)
        if current_price is not None:
            events += self._facts_under_new_snapshot(candidate, current_price, now_ms)
        return events

    def _invalidate_reference(self) -> None:
        self.snapshot = None
        self.invalid = True
        for rt in self.runtimes.values():
            rt.holds.clear()
            rt.recovery_t0 = None

    def _switch_reference(self, snapshot: ReferenceSnapshot) -> List[AlertEvent]:
        """整体切换：清候选与持续计时，episode 重新计（§4.2.6）。"""
        self.snapshot = snapshot
        self.invalid = False
        for rt in self.runtimes.values():
            rt.holds.clear()
            rt.episode_id = None
            rt.current_level = Level.IDLE
            rt.highest_level = Level.IDLE
            rt.critical_sent = False
            rt.sustained_after_critical = False
            rt.boundary_fact = False
            rt.last_urgent_ms = rt.last_notice_ms = rt.last_boundary_ms = None
            rt.recovery_t0 = None
            rt.entries = []
        return []

    def _facts_under_new_snapshot(self, snap: ReferenceSnapshot,
                                   price: Decimal, now_ms: int) -> List[AlertEvent]:
        """切换后当前价在新参考下已越界/极近/入缓冲：事件标注为参考切换。"""
        region = classify_region(snap, price)
        boundary_d = rules.boundary_direction(region)
        if boundary_d is not None:
            return self.on_fact(now_ms, boundary_d, BOUNDARY, price, switch=True)
        if region in (Region.BUFFER_DOWN, Region.BUFFER_UP):
            d = DOWN if region == Region.BUFFER_DOWN else rules.UP
            rt = self.runtimes[d]
            rt.entries.append(now_ms)
            u = rules.buffer_consumption(snap, price, d)
            if rules.critical_now(u, self.cfg):
                return self.on_fact(now_ms, d, CRITICAL, price, switch=True)
        return []

    # ───────────────────── 整秒评估 ─────────────────────

    def on_second(self, now_ms: int, slice_: FeatureSlice,
                  degraded: bool = False) -> List[AlertEvent]:
        """整秒决策点：VALID 评估三分支与恢复；DEGRADED/其余状态暂停提议、清计时。

        DEGRADED（§8.1）暂停依赖滚动窗口的普通/紧急规则并清持续计时；终止价
        事实与整秒补发仍由 on_fact/_boundary_repeat_at_second 处理，不受此限。
        """
        status = self.reference_status(now_ms)
        events: List[AlertEvent] = []
        for d in DIRECTIONS:
            rt = self.runtimes[d]
            events += self._boundary_repeat_at_second(rt, now_ms, slice_.price, status)
            if degraded or status != VALID or slice_.price is None:
                rt.holds.clear()
                rt.recovery_t0 = None
                continue
            preds = self._predicates(d, slice_)
            events += self._check_recovery(rt, preds, slice_.price, now_ms)
            if rt.episode_id is None and not any(preds.values()):
                continue
            firing = self._update_holds(rt, preds, now_ms)
            events += self._apply_firing_level(rt, firing, preds, now_ms, slice_.price)
        return events

    def _predicates(self, direction: str, slice_: FeatureSlice) -> Dict[str, bool]:
        snap = self.snapshot
        region = classify_region(snap, slice_.price)  # type: ignore[arg-type]
        g = grid_distance(snap, slice_.price, direction) if region == Region.INSIDE else None  # type: ignore[arg-type]
        return {
            NORMAL: rules.normal_candidate(direction, region, slice_, g, self.cfg),
            INSIDE_URGENT: rules.inside_urgent_candidate(
                direction, region, slice_, g, self.cfg),
            BUFFER_URGENT: rules.buffer_urgent_candidate(
                direction, region, slice_, self.cfg),
        }

    def _update_holds(self, rt: DirectionRuntime, preds: Dict[str, bool],
                      now_ms: int) -> Dict[str, bool]:
        """setdefault/pop 更新持续计时；返回已满持续的分支集合。"""
        firing: Dict[str, bool] = {}
        for branch in _HOLD_BRANCHES:
            if preds[branch]:
                rt.holds.setdefault(branch, now_ms)
                hold_s = self._hold_seconds(branch)
                t0 = rt.holds[branch]
                firing[branch] = t0 is not None and now_ms - t0 >= hold_s * MS_PER_SECOND
            else:
                rt.holds.pop(branch, None)
                firing[branch] = False
        return firing

    def _hold_seconds(self, branch: str) -> int:
        if branch == NORMAL:
            return self.cfg[NORMAL]["hold_seconds"]
        return self.cfg["urgent"]["hold_seconds"]

    def _apply_firing_level(self, rt: DirectionRuntime, firing: Dict[str, bool],
                            preds: Dict[str, bool], now_ms: int,
                            price: Decimal) -> List[AlertEvent]:
        sustained = [b for b in _URGENT_BRANCHES if firing[b]]
        events: List[AlertEvent] = []
        if sustained:
            events += self._urgent_events(rt, sustained, now_ms, price)
        elif firing[NORMAL]:
            events += self._notice_events(rt, now_ms, price)
        else:
            self._downgrade(rt, price)
        return events

    def _urgent_events(self, rt: DirectionRuntime, sustained: List[str],
                       now_ms: int, price: Decimal) -> List[AlertEvent]:
        rt.current_level = Level.URGENT
        if rt.highest_level != Level.URGENT:
            return self._send(rt, Level.URGENT, sustained[0], now_ms, price)
        if rt.critical_sent and not rt.sustained_after_critical:
            rt.sustained_after_critical = True
            return self._send(rt, Level.URGENT, sustained[0], now_ms, price)
        interval = self.cfg["repeat"]["urgent_seconds"] * MS_PER_SECOND
        if rt.last_urgent_ms is not None and now_ms - rt.last_urgent_ms >= interval:
            return self._send(rt, Level.URGENT, sustained[0], now_ms, price)
        return []

    def _notice_events(self, rt: DirectionRuntime, now_ms: int,
                       price: Decimal) -> List[AlertEvent]:
        if rt.current_level == Level.URGENT:
            rt.current_level = Level.NOTICE  # 紧急解除、普通仍满足：降级不发
            return []
        rt.current_level = Level.NOTICE
        interval = self.cfg["repeat"]["notice_seconds"] * MS_PER_SECOND
        if rt.last_notice_ms is not None and now_ms - rt.last_notice_ms < interval:
            return []
        return self._send(rt, Level.NOTICE, NORMAL, now_ms, price)

    def _downgrade(self, rt: DirectionRuntime, price: Decimal) -> None:
        """无满持续分支：价格仍越界→BOUNDARY_REACHED；有计时→CANDIDATE，否则 IDLE。

        边界事实已由 boundary_fact 独立锁存，级别下降不抹去该事实（§7.1）。
        """
        if self._is_beyond(rt.direction, price):
            rt.current_level = Level.BOUNDARY_REACHED
            return
        rt.current_level = Level.CANDIDATE if rt.holds else Level.IDLE

    def _is_beyond(self, direction: str, price: Decimal) -> bool:
        """价格是否在同方向终止价外（DOWN: p<=SL；UP: p>=SU）。"""
        snap = self.snapshot
        if direction == DOWN:
            return price <= snap.stop_lower
        return price >= snap.stop_upper

    # ───────────────────── 逐笔事实 ─────────────────────

    def on_fact(self, now_ms: int, direction: str, kind: str,
                 price: Decimal, gap_crossing: bool = False,
                 switch: bool = False) -> List[AlertEvent]:
        """逐笔事实：buffer_entry 记录；boundary VALID/STALE；critical VALID。"""
        status = self.reference_status(now_ms)
        rt = self.runtimes[direction]
        if kind == "buffer_entry":
            if not rt.entries or rt.entries[-1] != now_ms:
                rt.entries.append(now_ms)
            return []
        if kind == BOUNDARY and status in (VALID, STALE):
            return self._boundary_fact(rt, now_ms, price, status == STALE,
                                      gap_crossing, switch)
        if kind == CRITICAL and status == VALID:
            return self._critical_fact(rt, now_ms, price, switch)
        return []

    def _boundary_repeat_at_second(self, rt: DirectionRuntime, now_ms: int,
                                    price: Optional[Decimal],
                                    status: str) -> List[AlertEvent]:
        """价格仍越界时，按 boundary_seconds 在整秒点补发（VALID/STALE）。"""
        if not rt.boundary_fact or price is None or status not in (VALID, STALE):
            return []
        interval = self.cfg["repeat"]["boundary_seconds"] * MS_PER_SECOND
        if not self._is_beyond(rt.direction, price) or rt.last_boundary_ms is None:
            return []
        if now_ms - rt.last_boundary_ms < interval:
            return []
        rt.last_boundary_ms = now_ms
        return self._send(rt, Level.BOUNDARY_REACHED, BOUNDARY, now_ms, price,
                         stale=status == STALE)

    def _boundary_fact(self, rt: DirectionRuntime, now_ms: int, price: Decimal,
                        stale: bool, gap_crossing: bool,
                        switch: bool) -> List[AlertEvent]:
        self._ensure_episode(rt, now_ms)
        rt.current_level = Level.BOUNDARY_REACHED
        if rt.boundary_fact:
            interval = self.cfg["repeat"]["boundary_seconds"] * MS_PER_SECOND
            if rt.last_boundary_ms is None or now_ms - rt.last_boundary_ms < interval:
                return []
        rt.boundary_fact = True
        rt.last_boundary_ms = now_ms
        return self._send(rt, Level.BOUNDARY_REACHED, BOUNDARY, now_ms, price,
                         stale=stale, gap_crossing=gap_crossing, switch=switch)

    def _critical_fact(self, rt: DirectionRuntime, now_ms: int, price: Decimal,
                        switch: bool) -> List[AlertEvent]:
        if rt.critical_sent:
            return []
        self._ensure_episode(rt, now_ms)
        rt.critical_sent = True
        rt.current_level = Level.URGENT
        return self._send(rt, Level.URGENT, CRITICAL, now_ms, price, switch=switch)

    # ───────────────────── 恢复与发送 ─────────────────────

    def _check_recovery(self, rt: DirectionRuntime, preds: Dict[str, bool],
                        price: Decimal, now_ms: int) -> List[AlertEvent]:
        """完整恢复：严格区间内 g≥inside_fraction 且三分支均不成立，满 hold 关 episode。"""
        snap = self.snapshot
        region = classify_region(snap, price)
        if region != Region.INSIDE or any(preds.values()):
            rt.recovery_t0 = None
            return []
        g = grid_distance(snap, price, rt.direction)
        if g < self.cfg["recovery"]["inside_fraction"]:
            rt.recovery_t0 = None
            return []
        rt.recovery_t0 = rt.recovery_t0 or now_ms
        hold = self.cfg["recovery"]["hold_seconds"] * MS_PER_SECOND
        if now_ms - rt.recovery_t0 < hold:
            return []
        self._reset_after_recovery(rt)
        return []

    def _reset_after_recovery(self, rt: DirectionRuntime) -> None:
        rt.episode_id = None
        rt.holds.clear()
        rt.current_level = rt.highest_level = Level.IDLE
        rt.critical_sent = rt.sustained_after_critical = rt.boundary_fact = False
        rt.last_urgent_ms = rt.last_notice_ms = rt.last_boundary_ms = None
        rt.recovery_t0 = None

    def _ensure_episode(self, rt: DirectionRuntime, now_ms: int) -> None:
        if rt.episode_id is not None:
            return
        rt.episode_serial += 1
        rt.episode_id = (f"{self.snapshot.reference_id}|{rt.direction}"
                          f"|#{rt.episode_serial}")

    def _send(self, rt: DirectionRuntime, level: Level, reason: str,
              now_ms: int, price: Decimal, *, stale: bool = False,
              gap_crossing: bool = False, switch: bool = False) -> List[AlertEvent]:
        self._ensure_episode(rt, now_ms)
        rt.highest_level = level
        t0 = rt.holds.get(reason)
        event_id = (f"{self.snapshot.reference_id}|{rt.direction}"
                     f"|{rt.episode_serial}|{level.value}|{reason}")
        event = AlertEvent(
            event_id=event_id, episode_id=rt.episode_id,
            reference_id=self.snapshot.reference_id, direction=rt.direction,
            level=level.value, reason=reason, decision_ms=now_ms, price=price,
            first_held_ms=t0, gap_crossing=gap_crossing, stale=stale,
            reference_switch=switch)
        if level == Level.URGENT:
            rt.last_urgent_ms = now_ms
        elif level == Level.NOTICE:
            rt.last_notice_ms = now_ms
        self.events.append(event)
        return [event]
