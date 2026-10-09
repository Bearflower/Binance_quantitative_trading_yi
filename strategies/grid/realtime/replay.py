"""离线回放：读历史成交/样本 → 同一规则引擎 → 事件与实验指标（无网络/无交易）。

口径权威：需求 §10、计划 §2.4/§3。
- 来源：20 分钟样本 JSON（校验 SHA-256）或行情库（mode=ro 只读）。
- 逐笔事实只在状态跃迁时生成（入缓冲/越界/进入临界），毫秒级精度；终止价
  重复提醒在整秒点按间隔补发。
- 实验：10 个 profile（有/无效率过滤 × stride 1/3/5），输出提前量、可操作
  覆盖率、误报代理与告警负担，固定结果落盘。

CLI：
  python3 -m strategies.grid.realtime.replay audit
  python3 -m strategies.grid.realtime.replay experiment
  python3 -m strategies.grid.realtime.replay replay --profile-id research_seed_02.e60.s1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

from .features import (AggTrade, MS_PER_SECOND, SecondSample,
                      compute_features, efficiency, resampled_q,
                      sample_from_trades)
from .rules import (DOWN, UP, DIRECTIONS, Level, ReferenceSnapshot, Region,
                    buffer_consumption, classify_region, grid_distance, parse_profile)
from .state import (BOUNDARY, CRITICAL, RealtimeEngine)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_JSON = ROOT / "ethusdt_aggtrades_20261007_0950_1010.json"
DEFAULT_DB = ROOT / "data/aggtrades/ethusdt_aggtrades.sqlite"
DEFAULT_PROFILES = ROOT / "docs/research/grid_realtime/profiles.yaml"
DEFAULT_RESULTS = ROOT / "docs/research/grid_realtime/m1_experiment_results.json"
JSON_SHA256 = "63e375fb1cb7586c03c303682cab6fde389c94165cbbb67735f05064a04ec2fc"
ENTRY = "buffer_entry"


@dataclass(frozen=True)
class Fact:
    """逐笔状态跃迁事实。"""

    t: int
    direction: str
    kind: str
    price: Decimal
    gap: bool = False


# ───────────────────────── 数据加载 ─────────────────────────

def load_trades_json(path: Path, expected_sha256: Optional[str] = None) -> List[AggTrade]:
    """读取 20 分钟样本文件；expected_sha256 非空时必须一致（AC-31）。"""
    if not path.is_file():
        raise FileNotFoundError(f"缺数据文件: {path}")
    raw_bytes = path.read_bytes()
    if expected_sha256 is not None and hashlib.sha256(raw_bytes).hexdigest() != expected_sha256:
        raise ValueError(f"输入 SHA-256 与证据不符: {path}")
    trades: List[AggTrade] = []
    for row in json.loads(raw_bytes.decode("utf-8")):
        trades.append(AggTrade(
            agg_trade_id=row["aggTradeId"], price=Decimal(row["price"]),
            quantity=Decimal(row["quantity"]), trade_time_ms=row["trade_time_ms"],
            is_buyer_maker=1 if row["is_buyer_maker"] else 0))
    return trades


def _open_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"缺行情库: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_samples_db(path: Path, symbol: str) -> List[SecondSample]:
    """读整秒样本（稠密），在特征层落实 O-C3：锚距>max_gap → price=None。"""
    conn = _open_ro(path)
    try:
        rows = conn.execute(
            "SELECT sample_ms, price, anchor_time_ms, last_trade_id "
            "FROM price_samples_1s WHERE symbol=? ORDER BY sample_ms", (symbol,))
        samples: List[SecondSample] = []
        for t, p, anchor, trade_id in rows:
            samples.append(SecondSample(t, Decimal(p), anchor, trade_id))
        return samples
    finally:
        conn.close()


def apply_anchor_gap(samples: List[SecondSample], max_gap_seconds: int) -> None:
    """就地落实 O-C3：sample_ms-anchor_time_ms > max_gap → price 记 None。"""
    max_gap_ms = max_gap_seconds * MS_PER_SECOND
    for i, s in enumerate(samples):
        if s.sample_ms - s.anchor_time_ms > max_gap_ms:
            samples[i] = SecondSample(s.sample_ms, None, s.anchor_time_ms,
                                     s.anchor_trade_id)


def seconds_from_trades(trades: Sequence[AggTrade], max_gap_seconds: int) -> List[SecondSample]:
    """按审计脚本口径从逐笔生成稠密整秒样本（起点 floor(首T)+1s，上界 floor(末T)）。"""
    times = [t.trade_time_ms for t in trades]
    ids = [t.agg_trade_id for t in trades]
    prices = [t.price for t in trades]
    max_gap_ms = max_gap_seconds * MS_PER_SECOND
    return [sample_from_trades(times, ids, prices, t, max_gap_ms)
            for t in range(times[0] // MS_PER_SECOND * MS_PER_SECOND + MS_PER_SECOND,
                           times[-1] // MS_PER_SECOND * MS_PER_SECOND + 1, MS_PER_SECOND)]


# ───────────────────────── 逐笔事实扫描（跃迁式） ─────────────────────────

class FactScanner:
    """逐笔扫描，只在状态跃迁时产出事实（避免在越界期产生事实洪泛）。"""

    def __init__(self, snap: ReferenceSnapshot, critical_fraction: Decimal):
        self.snap = snap
        self.crit = critical_fraction
        self.prev: Optional[Decimal] = None

    def feed(self, t: int, price: Decimal) -> List[Fact]:
        facts: List[Fact] = []
        if self.prev is not None:
            facts += self._entries(t, price)
            facts += self._critical(t, price)
            facts += self._boundary(t, price)
        self.prev = price
        return facts

    def _entries(self, t: int, price: Decimal) -> List[Fact]:
        s = self.snap
        facts: List[Fact] = []
        if self.prev > s.grid_lower and price <= s.grid_lower:
            facts.append(Fact(t, DOWN, ENTRY, price))
        if self.prev < s.grid_upper and price >= s.grid_upper:
            facts.append(Fact(t, UP, ENTRY, price))
        return facts

    def _critical(self, t: int, price: Decimal) -> List[Fact]:
        s = self.snap
        u_d = buffer_consumption(s, price, DOWN)
        u_u = buffer_consumption(s, price, UP)
        prev_d = buffer_consumption(s, self.prev, DOWN)
        prev_u = buffer_consumption(s, self.prev, UP)
        facts: List[Fact] = []
        if prev_d < self.crit and self.crit <= u_d < 1:
            facts.append(Fact(t, DOWN, CRITICAL, price))
        if prev_u < self.crit and self.crit <= u_u < 1:
            facts.append(Fact(t, UP, CRITICAL, price))
        return facts

    def _boundary(self, t: int, price: Decimal) -> List[Fact]:
        s = self.snap
        facts: List[Fact] = []
        if self.prev > s.stop_lower and price <= s.stop_lower:
            facts.append(Fact(t, DOWN, BOUNDARY, price, gap=self.prev > s.grid_lower))
        if self.prev < s.stop_upper and price >= s.stop_upper:
            facts.append(Fact(t, UP, BOUNDARY, price, gap=self.prev < s.grid_upper))
        return facts


def scan_facts_trades(trades: Sequence[AggTrade], snap: ReferenceSnapshot,
                      critical_fraction: Decimal) -> List[Fact]:
    scanner = FactScanner(snap, critical_fraction)
    facts: List[Fact] = []
    for tr in trades:
        facts += scanner.feed(tr.trade_time_ms, tr.price)
    return facts


def scan_facts_db(path: Path, symbol: str, snap: ReferenceSnapshot,
                   critical_fraction: Decimal) -> List[Fact]:
    """只读流式扫描全库逐笔，产出跃迁事实（不把逐笔载入内存）。"""
    scanner = FactScanner(snap, critical_fraction)
    facts: List[Fact] = []
    conn = _open_ro(path)
    try:
        rows = conn.execute(
            "SELECT trade_time_ms, price FROM agg_trades WHERE symbol=? "
            "ORDER BY trade_time_ms, agg_trade_id", (symbol,))
        for t, p in rows:
            facts += scanner.feed(t, Decimal(p))
    finally:
        conn.close()
    return facts


# ───────────────────────── 引擎驱动 ─────────────────────────

def _emit_fact(engine: RealtimeEngine, fact: Fact) -> None:
    engine.on_fact(fact.t, fact.direction, fact.kind, fact.price,
                   gap_crossing=fact.gap)


def drive_engine(cfg: Dict, snap: ReferenceSnapshot,
                 seconds: Sequence[SecondSample], facts: Sequence[Fact]) -> RealtimeEngine:
    """统一驱动：整秒点先处理 t 之前事实，再评估秒点，最后处理同毫秒事实。"""
    price_map = {s.sample_ms: s.price for s in seconds}
    price_of = price_map.get
    engine = RealtimeEngine(cfg, snap)
    fi = 0
    stride = cfg["features"]["e_resample_seconds"]
    for s in seconds:
        t = s.sample_ms
        while fi < len(facts) and facts[fi].t < t:
            _emit_fact(engine, facts[fi])
            fi += 1
        feat = compute_features(price_of, t, cfg["features"]["windows_seconds"],
                               stride, [300])
        engine.on_second(t, feat)
        while fi < len(facts) and facts[fi].t == t:
            _emit_fact(engine, facts[fi])
            fi += 1
    return engine


# ───────────────────────── 指标 ─────────────────────────

def _events_by_level(events: Sequence) -> Dict[str, int]:
    return dict(Counter(e.level for e in events))


def _lead_block(first_boundary_ms: Optional[int], events: Sequence) -> Dict:
    """相对首次越界，各级最早事件的提前量与扣人工延迟后的可操作性。"""
    if first_boundary_ms is None:
        return {"lead_seconds": {}, "actionable_seconds": {}}
    leads: Dict[str, float] = {}
    for e in events:
        if e.decision_ms <= first_boundary_ms and e.level not in leads:
            leads[e.level] = (first_boundary_ms - e.decision_ms) / MS_PER_SECOND
    actionable = {}
    for level, lead in leads.items():
        actionable[level] = {f"minus_{d}s": lead - d > 0 for d in (30, 60, 120)}
    return {"lead_seconds": leads, "actionable_seconds": actionable}


def _is_recovered(price: Decimal, snap: ReferenceSnapshot, direction: str,
                  inside_fraction: Decimal) -> bool:
    """价格回到该方向区间深处（g≥inside_fraction）；与 state 恢复口径一致。"""
    if classify_region(snap, price) != Region.INSIDE:
        return False
    return grid_distance(snap, price, direction) >= inside_fraction


def _false_proxy(notice_events: Sequence, urgent_events: Sequence,
                  seconds: Sequence[SecondSample], snap: ReferenceSnapshot,
                  inside_fraction: Decimal) -> Dict[str, Dict[str, int]]:
    """误报代理：普通提醒后观察窗内未入缓冲、未升级；恢复=误报，停留=未决，超数据=截断。"""
    urgent_ms = sorted(e.decision_ms for e in urgent_events)
    outcome: Dict[str, Dict[str, int]] = {}
    for window_min in (15, 30, 60):
        counts = {"false_alarm": 0, "unresolved": 0, "truncated": 0, "valid": 0}
        width = window_min * 60 * MS_PER_SECOND
        for e in notice_events:
            end = e.decision_ms + width
            counts[_notice_outcome(e, end, urgent_ms, seconds, snap, inside_fraction)] += 1
        outcome[f"{window_min}min"] = counts
    return outcome


def _notice_outcome(notice, end_ms, urgent_ms: Sequence[int],
                    seconds: Sequence[SecondSample], snap: ReferenceSnapshot,
                    inside_fraction: Decimal) -> str:
    window_prices = (s for s in seconds
                     if notice.decision_ms < s.sample_ms <= end_ms and s.price is not None)
    entered_buffer = False
    recovered = False
    for s in window_prices:
        region = classify_region(snap, s.price)
        if notice.direction == DOWN and region in (Region.BUFFER_DOWN, Region.BELOW_SL):
            entered_buffer = True
        if notice.direction == UP and region in (Region.BUFFER_UP, Region.ABOVE_SU):
            entered_buffer = True
        if _is_recovered(s.price, snap, notice.direction, inside_fraction):
            recovered = True
    escalated = any(notice.decision_ms < u <= end_ms for u in urgent_ms)
    if entered_buffer or escalated:
        return "valid"
    if recovered:
        return "false_alarm"
    if end_ms > seconds[-1].sample_ms:
        return "truncated"
    return "unresolved"


def summarize(engine: RealtimeEngine, seconds: Sequence[SecondSample]) -> Dict:
    """汇总单 profile：时间线、提前量、负担、误报代理。"""
    result: Dict[str, Dict] = {}
    for d in DIRECTIONS:
        rt = engine.runtimes[d]
        evs = [e for e in engine.events if e.direction == d]
        boundary = next((e for e in evs if e.level == Level.BOUNDARY_REACHED.value), None)
        critical = next((e for e in evs if e.level == Level.URGENT.value
                         and e.reason == CRITICAL), None)
        notices = [e for e in evs if e.level == Level.NOTICE.value]
        urgents = [e for e in evs if e.level == Level.URGENT.value]
        block = {
            "first_buffer_entry_ms": rt.entries[0] if rt.entries else None,
            "first_critical_ms": critical.decision_ms if critical else None,
            "first_boundary_ms": boundary.decision_ms if boundary else None,
            "episodes": rt.episode_serial,
            "events_by_level": _events_by_level(evs),
        }
        block.update(_lead_block(block["first_boundary_ms"], evs))
        block["false_proxy"] = _false_proxy(
            notices, urgents, seconds, engine.snapshot,
            engine.cfg["recovery"]["inside_fraction"])
        result[d] = block
    return {"directions": result, "events_total": len(engine.events)}


# ───────────────────────── 研究 profile 文件 ─────────────────────────

def _to_decimal(value) -> Decimal:
    return Decimal(str(value))


def build_snapshot(raw: Dict) -> ReferenceSnapshot:
    keys = ("grid_lower", "grid_upper", "stop_lower", "stop_upper",
            "stop_move_up_price", "stop_move_down_price", "atr",
            "adx_1h", "adx_4h")
    data = {key: (_to_decimal(raw[key]) if raw.get(key) is not None else None)
            for key in keys}
    return ReferenceSnapshot(
        reference_id=raw["reference_id"], symbol=raw["symbol"],
        calculated_at_ms=raw["calculated_at_ms"],
        effective_at_ms=raw["effective_at_ms"], **data,
        market_state=raw.get("market_state"),
        config_version=raw.get("config_version"),
        overrides_version=raw.get("overrides_version"),
        config_hash=raw.get("config_hash"), message_id=raw.get("message_id"),
        source=raw.get("source"))


def deep_merge(base: Dict, override: Dict) -> Dict:
    """递归合并配置（override 覆盖 base；新增键并入）。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_research_file(path: Path) -> Tuple[ReferenceSnapshot, Dict[str, Dict]]:
    """读取研究文件：证据快照 + 10 个校验通过的 profile。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    snap = build_snapshot(data["snapshot"])
    profiles: Dict[str, Dict] = {}
    for profile_id, override in data["profiles"].items():
        profiles[profile_id] = parse_profile(deep_merge(data["base"], override))
    return snap, profiles


# ───────────────────────── 审计对齐与实验 ─────────────────────────

FOUR_POINTS = (1791338222000, 1791338327000, 1791338392000, 1791338471000)
FOUR_E_300 = (0.346498, 0.245597, 0.251059, 0.324966)


def first_return_breach(trades: Sequence[AggTrade], window_ms: int,
                        threshold: Decimal) -> Optional[int]:
    """逐笔滚动：首次 adverse 跌幅达 threshold 的成交时间（AC-16 基线）。"""
    import bisect
    times = [t.trade_time_ms for t in trades]
    for tr in trades:
        ref_t = tr.trade_time_ms - window_ms
        i = bisect.bisect_right(times, ref_t) - 1
        if i >= 0 and trades[i].price > 0 and tr.price / trades[i].price - 1 <= -threshold:
            return tr.trade_time_ms
    return None


def run_audit(input_path: Path, profiles_path: Path) -> Dict:
    """M0 审计对齐：20 分钟样本 + e60.s1 基线 profile，复现全部固定值。"""
    trades = load_trades_json(input_path, JSON_SHA256)
    snap, profiles = load_research_file(profiles_path)
    cfg = profiles["research_seed_02.e60.s1"]
    seconds = seconds_from_trades(trades, cfg["features"]["max_anchor_gap_seconds"])
    facts = scan_facts_trades(trades, snap, cfg["urgent"]["critical_buffer_fraction"])
    engine = drive_engine(cfg, snap, seconds, facts)
    rt = engine.runtimes[DOWN]
    return _audit_block(engine, rt, trades, seconds, snap)


def _audit_block(engine, rt, trades, seconds, snap) -> Dict:
    price_map = {s.sample_ms: s.price for s in seconds}
    e_values = []
    for t in FOUR_POINTS:
        q = resampled_q(price_map.get, t, 300, 1)
        e_values.append(round(float(efficiency(q)), 6))
    first_5m = first_return_breach(trades, 5 * 60 * MS_PER_SECOND, Decimal("0.005"))
    first_3m = first_return_breach(trades, 3 * 60 * MS_PER_SECOND, Decimal("0.005"))
    boundary_ms = next(e.decision_ms for e in engine.events
                       if e.level == Level.BOUNDARY_REACHED.value)
    critical_ms = next(e.decision_ms for e in engine.events
                       if e.level == Level.URGENT.value and e.reason == CRITICAL)
    block = {
        "entry_ms": rt.entries[0], "critical_ms": critical_ms,
        "boundary_ms": boundary_ms,
        "critical_lead_seconds": (boundary_ms - critical_ms) / MS_PER_SECOND,
        "four_efficiency": e_values,
        "first_5m_0.5pct_ms": first_5m, "first_3m_0.5pct_ms": first_3m,
        "events_by_level": _events_by_level(engine.events),
    }
    _assert_audit(block)
    return block


def _assert_audit(block: Dict) -> None:
    assert block["entry_ms"] == 1791338474591
    assert block["critical_ms"] == 1791338477251
    assert block["boundary_ms"] == 1791338477997
    assert block["critical_lead_seconds"] == 0.746
    assert block["four_efficiency"] == list(FOUR_E_300)
    assert block["first_5m_0.5pct_ms"] == 1791338222054
    assert block["first_3m_0.5pct_ms"] == 1791338327096


def run_experiment(db_path: Path, profiles_path: Path) -> Dict:
    """10 profile × 全库回放（样本表+跃迁事实），输出指标与 §4.3 判定素材。"""
    snap, profiles = load_research_file(profiles_path)
    base_cfg = next(iter(profiles.values()))
    seconds = load_samples_db(db_path, snap.symbol)
    apply_anchor_gap(seconds, base_cfg["features"]["max_anchor_gap_seconds"])
    facts = scan_facts_db(db_path, snap.symbol, snap,
                           base_cfg["urgent"]["critical_buffer_fraction"])
    results: Dict[str, Dict] = {}
    for profile_id, cfg in profiles.items():
        engine = drive_engine(cfg, snap, seconds, facts)
        results[profile_id] = summarize(engine, seconds)
    return {"snapshot_id": snap.reference_id, "results": results}


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"不可序列化: {type(value)}")


def save_results(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                              default=_json_default, sort_keys=True) + "\n",
                    encoding="utf-8")


# ───────────────────────── CLI ─────────────────────────

def _print(title: str, data: Dict) -> None:
    print(f"===== {title} =====")
    print(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default,
                     sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_audit = sub.add_parser("audit", help="复现审计指纹/四点效率/0.746s")
    p_audit.add_argument("--input", type=Path, default=DEFAULT_JSON)
    p_audit.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    p_exp = sub.add_parser("experiment", help="10 profile 对照实验")
    p_exp.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_exp.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    p_exp.add_argument("--output", type=Path, default=DEFAULT_RESULTS)
    p_rep = sub.add_parser("replay", help="单 profile 回放")
    p_rep.add_argument("--profile-id", required=True)
    p_rep.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_rep.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    args = parser.parse_args(argv)
    if args.cmd == "audit":
        _print("审计对齐", run_audit(args.input, args.profiles))
    elif args.cmd == "experiment":
        data = run_experiment(args.db, args.profiles)
        save_results(args.output, data)
        _print(f"实验完成，结果已写入 {args.output}", data)
    else:
        snap, profiles = load_research_file(args.profiles)
        seconds = load_samples_db(args.db, snap.symbol)
        cfg = profiles[args.profile_id]
        apply_anchor_gap(seconds, cfg["features"]["max_anchor_gap_seconds"])
        facts = scan_facts_db(args.db, snap.symbol, snap,
                               cfg["urgent"]["critical_buffer_fraction"])
        engine = drive_engine(cfg, snap, seconds, facts)
        _print(args.profile_id, summarize(engine, seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())  # pragma: no cover
