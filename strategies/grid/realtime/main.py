"""实时预警独立进程入口：装配 feed/features/rules/state/delivery 并按秒编排。

口径权威：需求 §3/§4/§8、计划 §2。
- 每 wall-clock 秒一拍：同步参考五态 → 逐笔事实 → 整秒特征 → 状态机 →
  事件落库 → 投递泵。
- SYNC_UNCERTAIN：暂停操作建议；有合法历史快照只记录 HISTORICAL_REFERENCE_CROSSED，
  无则只报降级（AC-21/27）。
- 健康事件不受普通冷却阻挡；同原因去重，恢复发恢复事件（§4.4）。
- 记录决策延迟分位（p50/90/99/999），超 health.max_event_lag_seconds 降级。
- shadow：只记录；RESEARCH 为唯一受控外发（§9.3）。不调用任何交易接口。
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Deque, Dict, List, Optional

from . import feed as feed_mod
from .delivery import (RESEARCH, Delivery, WebhookSingleSender)
from .features import (MS_PER_SECOND, AggTrade, SecondSample,
                       compute_features)
from .reference_store import (ReferenceReader, SYNC_UNCERTAIN, check_schema,
                               connect)
from .replay import FactScanner
from .rules import (DOWN, Level, ReferenceSnapshot, boundary_direction,
                    classify_region)
from .state import RealtimeEngine

_BEIJING = timezone(timedelta(hours=8))
_TEMPLATE = "【实时预警】{level_text} · {direction_text} · {beijing}\n"


# ───────────────────── 延迟分位 ─────────────────────

class LatencyTracker:
    """滚动记录秒级延迟并输出分位数（最近秩）。"""

    def __init__(self, maxlen: int = 2000):
        self._samples: Deque[float] = deque(maxlen=maxlen)

    def record(self, seconds: float) -> None:
        self._samples.append(seconds)

    def quantiles(self) -> Dict[str, float]:
        ordered = sorted(self._samples)
        if not ordered:
            return {}
        levels = {"p50": 500, "p90": 900, "p99": 990, "p999": 999}
        return {key: round(_quantile(ordered, permille), 6)
                for key, permille in levels.items()}

    def p99(self) -> float:
        return _quantile(sorted(self._samples), 990) if self._samples else 0.0


def _quantile(ordered: List[float], permille: float) -> float:
    index = max(0, math.ceil(permille / 1000 * len(ordered)) - 1)
    return ordered[index]


# ───────────────────── 健康状态 ─────────────────────

class HealthMonitor:
    """按原因跟踪降级/恢复；每原因每次出现一条事件（同原因天然去重）。"""

    def __init__(self):
        self._active: Dict[str, int] = {}

    def update(self, reasons: Dict[str, int], now_ms: int,
               price: Decimal) -> List:
        events = []
        for reason, since in reasons.items():
            if reason not in self._active:
                self._active[reason] = since
                events.append(_health_event(reason, since, price, False))
        for gone in [r for r in self._active if r not in reasons]:
            events.append(_health_event(gone, now_ms, price, True))
            self._active.pop(gone)
        return events

    @property
    def active_reasons(self) -> Dict[str, int]:
        return dict(self._active)


def _health_event(reason: str, now_ms: int, price: Decimal,
                  recovered: bool):
    """构造健康事件（event_id 确定性；level=HEALTH）。"""
    from .state import AlertEvent
    tag = "recovered" if recovered else "degraded"
    return AlertEvent(
        event_id=f"health-{tag}-{reason}", episode_id=f"health-{reason}",
        reference_id="", direction="SYSTEM", level="HEALTH",
        reason=f"{tag}:{reason}", decision_ms=now_ms, price=price)


# ───────────────────── 实时服务 ─────────────────────

class RealtimeService:
    """在线/测试共用编排器（依赖注入，无内部硬编码业务参数）。"""

    def __init__(self, cfg: Dict, *, conn, trade_feed=None, sender=None,
                 sleep=asyncio.sleep, now_ms=feed_mod.now_ms):
        self._cfg = cfg
        self._conn = conn
        self._feed = trade_feed
        self._reader = ReferenceReader(conn, cfg)
        self._delivery = Delivery(conn, cfg)
        self._engine = RealtimeEngine(cfg)
        self._health = HealthMonitor()
        self._latency = LatencyTracker()
        self._samples: Deque[SecondSample] = deque(
            maxlen=cfg["reference"]["max_age_seconds"])
        self._scanner: Optional[FactScanner] = None
        self._trade_queue: Optional[asyncio.Queue] = None
        self._feed_healthy = True
        self._storage_degraded = False
        self._event_refs: Dict[str, tuple] = {}
        self._tick_count = 0
        self._sender = sender
        self._sleep = sleep
        self._now = now_ms

    async def run(self) -> None:
        """进程主循环（启动前只校验 schema，不迁移）。"""
        check_schema(self._conn)
        feed_task = await self._start_feed()
        period = self._cfg["features"]["sample_seconds"]
        try:
            while True:
                await self.tick()
                await self._sleep(period)
        finally:
            if feed_task is not None:
                feed_task.cancel()

    async def tick(self) -> Dict:
        """单拍：drain 成交 → 处理 → 维护延迟。返回量化快照（测试用）。"""
        started = time.monotonic()
        now = self._now()
        trades = self._drain_trades()
        await self.process(now, trades)
        self._latency.record(time.monotonic() - started)
        return self._snapshot()

    async def process(self, now_ms: int, trades: List[AggTrade]) -> None:
        """一拍全部业务（测试可直接驱动）。"""
        snap, status = self._reader.current_snapshot(now_ms)
        latest = _last_price(trades)
        events = self._sync_engine(snap, now_ms, latest)
        events += self._evaluate(now_ms, trades, snap, status)
        self._run_maintenance(now_ms)
        reasons = self._reasons(status)
        events += self._health.update(reasons, now_ms, latest or Decimal(0))
        await self._record_and_send(events, now_ms, snap)
        self._tick_count += 1

    # ─── 参考同步 ───

    def _sync_engine(self, snap: Optional[ReferenceSnapshot], now_ms: int,
                     latest: Optional[Decimal]) -> List:
        """同步最新参考；返回参考切换当下已产生的事件（必须记录，不能丢弃）。"""
        if snap is None:
            return []
        current = self._engine.snapshot
        if current is not None and current.reference_id == snap.reference_id:
            return []
        events = self._engine.apply_send_result("SENT", snap, now_ms, latest)
        self._scanner = FactScanner(
            snap, self._cfg["urgent"]["critical_buffer_fraction"])
        return events

    # ─── 评估 ───

    def _evaluate(self, now_ms: int, trades: List[AggTrade],
                  snap: Optional[ReferenceSnapshot], status: str) -> List:
        mark = now_ms // MS_PER_SECOND * MS_PER_SECOND
        self._add_sample(mark, trades)
        if status in ("VALID", "STALE") and snap is not None:
            return self._evaluate_tracked(mark, trades, snap, status)
        if status == SYNC_UNCERTAIN:
            return self._evaluate_historical(now_ms, trades)
        return []

    def _evaluate_tracked(self, mark: int, trades: List[AggTrade],
                          snap: ReferenceSnapshot, status: str) -> List:
        before = [t for t in trades if t.trade_time_ms < mark]
        same = [t for t in trades if t.trade_time_ms == mark]
        events = self._emit_facts(before, snap)
        features = compute_features(
            self._sample_price, mark, self._cfg["features"]["windows_seconds"],
            self._cfg["features"]["e_resample_seconds"], [300])
        events += self._engine.on_second(
            mark, features, degraded=not self._feed_healthy)
        events += self._emit_facts(same, snap)
        return events

    def _evaluate_historical(self, now_ms: int,
                             trades: List[AggTrade]) -> List:
        historical = self._reader.historical_snapshot()
        price = _fresh_price(trades, self._cfg)
        if historical is None or price is None:
            return []
        direction = boundary_direction(classify_region(historical, price))
        if direction is None:
            return []
        return [_historical_event(historical, direction, price, now_ms)]

    def _emit_facts(self, trades: List[AggTrade],
                    snap: ReferenceSnapshot) -> List:
        if self._scanner is None:
            self._scanner = FactScanner(
                snap, self._cfg["urgent"]["critical_buffer_fraction"])
        events = []
        for tr in trades:
            for fact in self._scanner.feed(tr.trade_time_ms, tr.price):
                events += self._engine.on_fact(
                    fact.t, fact.direction, fact.kind, fact.price,
                    gap_crossing=fact.gap)
        return events

    # ─── 采样序列 ───

    def _add_sample(self, mark: int, trades: List[AggTrade]) -> None:
        last = next((t for t in reversed(trades)
                     if t.trade_time_ms <= mark), None)
        sample = self._build_sample(mark, last)
        self._samples.append(sample)

    def _build_sample(self, mark: int,
                      last: Optional[AggTrade]) -> SecondSample:
        if last is not None:
            return SecondSample(mark, last.price, last.trade_time_ms,
                                last.agg_trade_id)
        if not self._samples:
            return SecondSample(mark, None, None, None)
        prev = self._samples[-1]
        price = prev.price if _gap_ok(prev, mark, self._cfg) else None
        return SecondSample(mark, price, prev.anchor_time_ms,
                            prev.anchor_trade_id)

    def _sample_price(self, sample_ms: int) -> Optional[Decimal]:
        for sample in reversed(self._samples):
            if sample.sample_ms == sample_ms:
                return sample.price
        return None

    # ─── 记录与投递 ───

    async def _record_and_send(self, events: List, now_ms: int,
                               snap: Optional[ReferenceSnapshot]) -> None:
        for event in events:
            await self._record_one(event, now_ms, snap)
        render = self._render_latch
        await self._delivery.pump(
            self._sender, now_ms, render=render,
            condition_holds=self._condition_holds)

    async def _record_one(self, event, now_ms: int,
                          snap: Optional[ReferenceSnapshot]) -> None:
        payload = build_payload(event, snap, self._cfg)
        config_hash = snap.config_hash if snap is not None else None
        self._event_refs[event.event_id] = (
            event.direction, event.episode_id, event.level)
        if self._cfg.get("mode") == "shadow" and event.level != "HEALTH":
            if self._delivery.record_research(
                    event, build_research_payload(event, snap), now_ms):
                return
            # research 开关关闭/被频率护栏拦截 → 只记录（SHADOW 行），不外发
        self._delivery.record(event, payload, now_ms, config_hash)

    def _condition_holds(self, event_id: str) -> bool:
        direction, episode_id, level = self._event_refs[event_id]
        if direction == "SYSTEM":
            return True
        runtime = self._engine.runtimes[direction]
        return (runtime.episode_id == episode_id
                and runtime.current_level == Level[level])

    def _render_latch(self, original: str, row, now_ms: int) -> str:
        """锁存事实发送文案：超时加补报标题；确认已反弹才附当前价（§8.2）。

        补报阈值取配置；研究参考行（§9.3）必须始终以「【研究参考】」开头，
        故不前置补报标题。
        """
        delay_ms = now_ms - row["locked_at_ms"]
        report_after = (self._cfg["delivery"]["report_delay_seconds"]
                        * MS_PER_SECOND)
        title = ("【历史触及事件补报】"
                 if row["kind"] != RESEARCH and delay_ms > report_after else "")
        return f"{title}{original}{self._rebound_line(row)}"

    def _rebound_line(self, row) -> str:
        """仅确认价格已回到参考区间内才写“曾于某时触及”；仍越界/不可判定则不附。"""
        ref = self._event_refs.get(row["event_id"])
        snap = self._engine.snapshot
        if not ref or ref[0] not in ("UP", "DOWN") or snap is None:
            return ""
        if ref[2] == "HISTORICAL_REFERENCE_CROSSED":
            return ""  # 历史参考线不据当前参考判定反弹
        latest = self._samples[-1].price if self._samples else None
        if latest is None:
            return ""
        beyond = (latest <= snap.stop_lower if ref[0] == "DOWN"
                  else latest >= snap.stop_upper)
        if beyond:
            return ""
        return f"\n曾于某时触及，当前价格已回到：{latest}"

    # ─── 健康原因与维护 ───

    def _reasons(self, status: str) -> Dict[str, int]:
        reasons: Dict[str, int] = {}
        now = self._now()
        if not self._feed_healthy:
            reasons["feed_interrupted"] = now
        if status == SYNC_UNCERTAIN:
            reasons["sync_uncertain"] = now
        elif status in ("MISSING", "INVALID"):
            reasons[f"reference_{status.lower()}"] = now
        if self._storage_degraded:
            reasons["storage_degraded"] = now
        if self._latency.p99() > self._cfg["health"]["max_event_lag_seconds"]:
            reasons["processing_lag"] = now
        return reasons

    def _run_maintenance(self, now_ms: int) -> None:
        interval = self._cfg["delivery"]["maintenance_interval_seconds"]
        if self._tick_count % interval != 0:
            return
        result = self._delivery.maintenance(now_ms)
        self._storage_degraded = result["storage_degraded"]

    # ─── feed 接入 ───

    async def _start_feed(self):
        if self._feed is None:
            self._feed = feed_mod.TradeFeed(
                self._cfg, on_health=self._on_feed_health)
        self._trade_queue = asyncio.Queue(
            maxsize=self._cfg["delivery"]["queue_capacity"])
        return asyncio.create_task(self._pump_feed())

    async def _pump_feed(self) -> None:
        async for trade in self._feed.stream():  # pragma: no branch
            if self._trade_queue.full():
                self._storage_degraded = True
                continue
            self._trade_queue.put_nowait(trade)

    def _drain_trades(self) -> List[AggTrade]:
        if self._trade_queue is None:
            return []
        trades = []
        while not self._trade_queue.empty():
            trades.append(self._trade_queue.get_nowait())
        return trades

    async def _on_feed_health(self, healthy: bool, reason: str) -> None:
        self._feed_healthy = healthy

    def _snapshot(self) -> Dict:
        """运行态量化快照（测试断言与运维巡检用）。"""
        return {
            "reference_id": self._engine.snapshot.reference_id
            if self._engine.snapshot else None,
            "events_total": len(self._engine.events),
            "health": self._health.active_reasons,
            "latency": self._latency.quantiles(),
            "feed_healthy": self._feed_healthy,
            "storage_degraded": self._storage_degraded}


# ───────────────────── 文案构造 ─────────────────────

def build_payload(event, snap: Optional[ReferenceSnapshot], cfg: Dict) -> str:
    """按 §8.3 组装消息字段（健康事件走降级模板）。"""
    beijing = _beijing(event.decision_ms)
    if event.level == "HEALTH":
        return f"【系统健康】{event.reason} · {beijing}"
    level_text = _level_text(event.level)
    direction_text = "下行" if event.direction == DOWN else "上行"
    lines = [_TEMPLATE.format(
        level_text=level_text, direction_text=direction_text, beijing=beijing)]
    if snap is not None:
        lines += _reference_lines(snap)
    lines.append(f"当前成交价（成交价口径）：{event.price}")
    lines.append(f"触发原因：{event.reason}")
    return "\n".join(lines)


def build_research_payload(event, snap: Optional[ReferenceSnapshot]) -> str:
    """§9.3 研究参考级提醒：研究反馈通道，不套用 §8.3 操作建议模板。"""
    direction_text = "下行" if event.direction == DOWN else "上行"
    lines = [
        "【研究参考】未经样本外验证，可能误报或漏报，不构成操作建议",
        f"【研究观测】{_research_level_text(event.level)} · {direction_text} · "
        f"{_beijing(event.decision_ms)}"]
    if snap is not None:
        lines += _research_reference_lines(snap, event.level)
    lines.append(f"当前成交价（成交价口径）：{event.price}")
    lines.append(f"触发原因：{event.reason}")
    return "\n".join(lines)


def _research_reference_lines(snap: ReferenceSnapshot, level: str) -> List[str]:
    """研究标注用参考字段；避免“建议终止价”等操作指令语义（§8.3/§9.3）。"""
    prefix = "历史" if level == "HISTORICAL_REFERENCE_CROSSED" else ""
    return [
        f"{prefix}参考时间：{_beijing(snap.effective_at_ms)}",
        f"{prefix}参考区间：{snap.grid_lower}～{snap.grid_upper}",
        f"{prefix}参考线：{snap.stop_lower} / {snap.stop_upper}",
        f"参考版本：{snap.reference_id}"]


def _reference_lines(snap: ReferenceSnapshot) -> List[str]:
    beijing = _beijing(snap.effective_at_ms)
    return [
        f"最新建议时间：{beijing}",
        f"建议区间：{snap.grid_lower}～{snap.grid_upper}",
        f"建议终止价：{snap.stop_lower} / {snap.stop_upper}",
        f"参考版本：{snap.reference_id}"]


def _historical_event(snap: ReferenceSnapshot, direction: str,
                      price: Decimal, now_ms: int):
    """构造历史参考穿越事件（不进升级链；event_id 按参考+方向去重）。"""
    from .state import AlertEvent
    return AlertEvent(
        event_id=f"hist-{snap.reference_id}-{direction}",
        episode_id=f"hist-{snap.reference_id}",
        reference_id=snap.reference_id, direction=direction,
        level="HISTORICAL_REFERENCE_CROSSED",
        reason="historical_reference_crossed",
        decision_ms=now_ms, price=price)


_LEVEL_TEXT = {
    "NOTICE": "异动关注", "URGENT": "风险升级",
    "BOUNDARY_REACHED": "已触及/越过建议终止价",
    "HISTORICAL_REFERENCE_CROSSED": "历史参考穿越"}

# 研究参考通道：显式白名单，仅复用中性级别，不继承 §8.3 的指令语义文案（§9.3）
_RESEARCH_LEVEL_TEXT = {
    "NOTICE": _LEVEL_TEXT["NOTICE"], "URGENT": _LEVEL_TEXT["URGENT"],
    "BOUNDARY_REACHED": "越界事实",
    "HISTORICAL_REFERENCE_CROSSED": _LEVEL_TEXT["HISTORICAL_REFERENCE_CROSSED"]}


def _level_text(level: str) -> str:
    return _LEVEL_TEXT.get(level, level)


def _research_level_text(level: str) -> str:
    """研究参考级别文案：中性描述，不含“建议终止”等指令语义（§9.3）。"""
    return _RESEARCH_LEVEL_TEXT.get(level, level)


def _beijing(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=_BEIJING).strftime(
        "%Y-%m-%d %H:%M:%S")


def _last_price(trades: List[AggTrade]) -> Optional[Decimal]:
    return trades[-1].price if trades else None


def _fresh_price(trades: List[AggTrade], cfg: Dict) -> Optional[Decimal]:
    """最新成交价（行情必须新鲜；无成交返回 None）。"""
    if not trades:
        return None
    latest = trades[-1]
    gap = cfg["features"]["max_anchor_gap_seconds"] * MS_PER_SECOND
    return latest.price if feed_mod.now_ms() - latest.trade_time_ms <= gap else None


def _gap_ok(prev: SecondSample, mark: int, cfg: Dict) -> bool:
    if prev.anchor_time_ms is None:
        return False
    max_gap = cfg["features"]["max_anchor_gap_seconds"] * MS_PER_SECOND
    return mark - prev.anchor_time_ms <= max_gap


# ───────────────────── 进程入口 ─────────────────────

def _load_cfg() -> Dict:
    """从策略配置 realtime_alert 节解析（含全量 §9.2 校验）。"""
    from .rules import parse_profile
    strategy_dir = os.path.dirname(os.path.dirname(__file__))
    import yaml
    config_path = os.path.join(strategy_dir, "config.yaml")
    with open(config_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return parse_profile(raw["realtime_alert"])


def main(argv=None) -> int:
    """CLI：python -m strategies.grid.realtime.main [--db-path PATH]"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=None,
                        help="覆盖 storage.path（须绝对路径）")
    args = parser.parse_args(argv)
    cfg = _load_cfg()
    db_path = args.db_path or cfg["storage"]["path"]
    conn = connect(db_path, cfg["storage"]["busy_timeout_ms"])
    # alert 模式需要发送；shadow 下开启 research 通道（§9.3）同样需要真实 sender
    needs_sender = (cfg.get("mode") == "alert"
                    or cfg.get("shadow_research", {}).get("enabled"))
    sender = WebhookSingleSender() if needs_sender else None
    service = RealtimeService(cfg, conn=conn, sender=sender)
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
