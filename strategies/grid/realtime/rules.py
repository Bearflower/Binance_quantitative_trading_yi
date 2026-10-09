"""规则层（纯函数；禁止 import pandas/网络/系统时钟）。

口径权威：需求 §4/§6/§9、计划 §2.3。
- 参考快照合法性：0 < stop_lower < grid_lower < grid_upper < stop_upper。
- 区域五分类、缓冲消耗 u、区间距离 g，上下行对称。
- 普通异动、区间内紧急、缓冲紧急、极近边界、终止价穿越五类判定。
- profile 解析与校验：完整落实 §9.2 校验要求，缺字段/越界即拒绝加载。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, Optional

from .features import FeatureSlice, MS_PER_SECOND

DOWN = "DOWN"
UP = "UP"
DIRECTIONS = (DOWN, UP)


class Region(Enum):
    INSIDE = "INSIDE"
    BUFFER_DOWN = "BUFFER_DOWN"
    BUFFER_UP = "BUFFER_UP"
    BELOW_SL = "BELOW_SL"
    ABOVE_SU = "ABOVE_SU"


class Level(Enum):
    IDLE = "IDLE"
    CANDIDATE = "CANDIDATE"
    NOTICE = "NOTICE"
    URGENT = "URGENT"
    BOUNDARY_REACHED = "BOUNDARY_REACHED"


@dataclass(frozen=True)
class ReferenceSnapshot:
    """小时出口成功推送的建议快照（需求 §4.1）；stop_move 仅溯源，不参与判定。"""

    reference_id: str
    symbol: str
    calculated_at_ms: int
    effective_at_ms: int
    grid_lower: Decimal
    grid_upper: Decimal
    stop_lower: Decimal
    stop_upper: Decimal
    stop_move_up_price: Optional[Decimal] = None
    stop_move_down_price: Optional[Decimal] = None
    market_state: Optional[str] = None
    atr: Optional[Decimal] = None
    adx_1h: Optional[Decimal] = None
    adx_4h: Optional[Decimal] = None
    config_version: Optional[str] = None
    overrides_version: Optional[str] = None
    config_hash: Optional[str] = None
    message_id: Optional[str] = None
    source: Optional[str] = None


# ───────────────────────── 快照与几何 ─────────────────────────

def validate_snapshot(snap: ReferenceSnapshot,
                       expected_symbol: Optional[str] = None) -> None:
    """校验快照身份、时间与四边界严格序（非法快照不得激活，§4.1）。"""
    if not snap.reference_id or not snap.symbol:
        raise ValueError("快照 reference_id/symbol 不能为空")
    if expected_symbol is not None and snap.symbol != expected_symbol:
        raise ValueError(f"快照品种 {snap.symbol} 与配置 {expected_symbol} 不一致")
    if snap.calculated_at_ms <= 0 or snap.effective_at_ms <= 0:
        raise ValueError("快照计算/生效时间必须为正")
    values = (snap.stop_lower, snap.grid_lower, snap.grid_upper, snap.stop_upper)
    if any(not v.is_finite() for v in values):
        raise ValueError("快照边界必须为有限数")
    if not 0 < snap.stop_lower < snap.grid_lower < snap.grid_upper < snap.stop_upper:
        raise ValueError("快照边界必须满足 0 < stop_lower < grid_lower < grid_upper < stop_upper")


def classify_region(snap: ReferenceSnapshot, price: Decimal) -> Region:
    """五区域分类；恰等于 L 归下方缓冲区，恰等于 U 归上方缓冲区（需求 §6.1）。"""
    if price <= snap.stop_lower:
        return Region.BELOW_SL
    if price <= snap.grid_lower:
        return Region.BUFFER_DOWN
    if price < snap.grid_upper:
        return Region.INSIDE
    if price < snap.stop_upper:
        return Region.BUFFER_UP
    return Region.ABOVE_SU


def buffer_consumption(snap: ReferenceSnapshot, price: Decimal,
                       direction: str) -> Decimal:
    """缓冲消耗 u：down=(L-p)/(L-SL)，up=(p-U)/(SU-U)。"""
    if direction == DOWN:
        return (snap.grid_lower - price) / (snap.grid_lower - snap.stop_lower)
    return (price - snap.grid_upper) / (snap.stop_upper - snap.grid_upper)


def grid_distance(snap: ReferenceSnapshot, price: Decimal,
                  direction: str) -> Decimal:
    """区间内相对边界距离 g（仅严格区间内有定义）。"""
    width = snap.grid_upper - snap.grid_lower
    if direction == DOWN:
        return (price - snap.grid_lower) / width
    return (snap.grid_upper - price) / width


def adverse(direction: str, r_value: Optional[Decimal]) -> Optional[Decimal]:
    """同方向不利变动：down 取 -r，up 取 r；None 透传。"""
    if r_value is None:
        return None
    return -r_value if direction == DOWN else r_value


def _ge(value: Optional[Decimal], threshold: Decimal) -> bool:
    """带 None 保护的门槛比较（特征不可用即不成立）。"""
    return value is not None and value >= threshold


def _direction_buffer(direction: str, region: Region) -> bool:
    return region == (Region.BUFFER_DOWN if direction == DOWN else Region.BUFFER_UP)


# ───────────────────────── 分支判定 ─────────────────────────

def _efficiency_ok(slice_: FeatureSlice, threshold: Decimal,
                    filter_enabled: bool) -> bool:
    """效率门槛：关闭过滤时不判 E；开启时 E 不可用即不成立。"""
    if not filter_enabled:
        return True
    return _ge(slice_.e[300], threshold)


def normal_candidate(direction: str, region: Region, slice_: FeatureSlice,
                     g: Optional[Decimal], cfg: Dict) -> bool:
    """普通异动：变化成立(3m/5m OR) AND 方向持续(E) AND 位置相关（§6.2）。"""
    normal = cfg["normal"]
    a180 = adverse(direction, slice_.r[180])
    a300 = adverse(direction, slice_.r[300])
    change = _ge(a180, normal["return_3m"]) or _ge(a300, normal["return_5m"])
    in_position = _direction_buffer(direction, region) or (
        region == Region.INSIDE and g is not None and g <= normal["near_grid_fraction"])
    return change and in_position and _efficiency_ok(
        slice_, normal["min_efficiency"], cfg["efficiency_filter"]["enabled"])


def inside_urgent_candidate(direction: str, region: Region, slice_: FeatureSlice,
                            g: Optional[Decimal], cfg: Dict) -> bool:
    """区间内快速恶化：近边界 AND 3m/5m OR AND E（§6.3 分支一）。"""
    urgent = cfg["urgent"]
    near = region == Region.INSIDE and g is not None and g <= urgent["near_grid_fraction"]
    a180 = adverse(direction, slice_.r[180])
    a300 = adverse(direction, slice_.r[300])
    change = _ge(a180, urgent["return_3m"]) or _ge(a300, urgent["return_5m"])
    return near and change and _efficiency_ok(
        slice_, urgent["min_efficiency"], cfg["efficiency_filter"]["enabled"])


def buffer_urgent_candidate(direction: str, region: Region,
                            slice_: FeatureSlice, cfg: Dict) -> bool:
    """缓冲区继续恶化：同向缓冲内 AND a60 达 1m 门槛（§6.3 分支二）。"""
    if not _direction_buffer(direction, region):
        return False
    a60 = adverse(direction, slice_.r[60])
    return _ge(a60, cfg["urgent"]["buffer_return_1m"])


def critical_now(u: Optional[Decimal], cfg: Dict) -> bool:
    """极近边界：critical_fraction <= u < 1（逐笔，无持续/涨跌要求，§6.3 分支三）。"""
    if u is None:
        return False
    return cfg["urgent"]["critical_buffer_fraction"] <= u < 1


def boundary_direction(region: Region) -> Optional[str]:
    """逐笔终止价穿越方向；未越界返回 None（§6.4）。"""
    if region == Region.BELOW_SL:
        return DOWN
    if region == Region.ABOVE_SU:
        return UP
    return None


def pick_state(levels: Iterable[Level]) -> Level:
    """按 BOUNDARY→URGENT→NOTICE→CANDIDATE→IDLE 自上而下取级。"""
    for level in (Level.BOUNDARY_REACHED, Level.URGENT, Level.NOTICE,
                   Level.CANDIDATE):
        if level in levels:
            return level
    return Level.IDLE


# ───────────────────────── profile 解析与校验 ─────────────────────────

_REQUIRED_SECTIONS = ("reference", "features", "normal", "urgent", "recovery",
                       "repeat", "health", "storage", "transport",
                       "reference_sync", "delivery")
_FIXED_WINDOWS = (60, 180, 300)


def _dec(value, key: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        raise ValueError(f"{key} 必须是数值，实际: {value!r}")
    if not parsed.is_finite():
        raise ValueError(f"{key} 必须为有限数，实际: {value!r}")
    return parsed


def _positive(value, key: str) -> Decimal:
    parsed = _dec(value, key)
    if parsed <= 0:
        raise ValueError(f"{key} 必须为正数，实际: {value!r}")
    return parsed


def _positive_int(value, key: str) -> int:
    parsed = _positive(value, key)
    if parsed != int(parsed):
        raise ValueError(f"{key} 必须为正整数，实际: {value!r}")
    return int(parsed)


def _fraction(value, key: str) -> Decimal:
    """(0,1] 区间比例。"""
    parsed = _positive(value, key)
    if parsed > 1:
        raise ValueError(f"{key} 必须位于 (0,1]，实际: {value!r}")
    return parsed


def _inside_fraction(value, key: str) -> Decimal:
    """(0,0.5) 区间内接近比例。"""
    parsed = _positive(value, key)
    if parsed >= Decimal("0.5"):
        raise ValueError(f"{key} 必须位于 (0,0.5)，实际: {value!r}")
    return parsed


def _parse_features(raw: Dict) -> Dict:
    if raw.get("sample_seconds") != 1:
        raise ValueError("features.sample_seconds 固定为 1，不得修改")
    windows = tuple(raw.get("windows_seconds"))
    if windows != _FIXED_WINDOWS:
        raise ValueError("features.windows_seconds 固定为 [60, 180, 300]，不得修改")
    stride = _positive_int(raw.get("e_resample_seconds", 1),
                          "features.e_resample_seconds")
    if 300 % stride != 0:
        raise ValueError("features.e_resample_seconds 必须整除 300")
    return {
        "sample_seconds": 1,
        "windows_seconds": windows,
        "max_anchor_gap_seconds": _positive_int(
            raw.get("max_anchor_gap_seconds"), "features.max_anchor_gap_seconds"),
        "e_resample_seconds": stride,
    }


def _parse_normal(raw: Dict) -> Dict:
    return {
        "return_3m": _positive(raw.get("return_3m"), "normal.return_3m"),
        "return_5m": _positive(raw.get("return_5m"), "normal.return_5m"),
        "min_efficiency": _fraction(raw.get("min_efficiency"), "normal.min_efficiency"),
        "near_grid_fraction": _inside_fraction(
            raw.get("near_grid_fraction"), "normal.near_grid_fraction"),
        "hold_seconds": _positive_int(raw.get("hold_seconds"), "normal.hold_seconds"),
    }


def _parse_urgent(raw: Dict) -> Dict:
    return {
        "near_grid_fraction": _inside_fraction(
            raw.get("near_grid_fraction"), "urgent.near_grid_fraction"),
        "return_3m": _positive(raw.get("return_3m"), "urgent.return_3m"),
        "return_5m": _positive(raw.get("return_5m"), "urgent.return_5m"),
        "min_efficiency": _fraction(raw.get("min_efficiency"), "urgent.min_efficiency"),
        "hold_seconds": _positive_int(raw.get("hold_seconds"), "urgent.hold_seconds"),
        "buffer_return_1m": _positive(
            raw.get("buffer_return_1m"), "urgent.buffer_return_1m"),
        "critical_buffer_fraction": _fraction(
            raw.get("critical_buffer_fraction"), "urgent.critical_buffer_fraction"),
    }


def _parse_recovery(raw: Dict) -> Dict:
    return {
        "inside_fraction": _positive(raw.get("inside_fraction"), "recovery.inside_fraction"),
        "hold_seconds": _positive_int(raw.get("hold_seconds"), "recovery.hold_seconds"),
    }


def _parse_repeat(raw: Dict) -> Dict:
    return {key: _positive_int(raw.get(key), f"repeat.{key}")
            for key in ("notice_seconds", "urgent_seconds", "boundary_seconds",
                         "health_seconds")}


def _parse_health(raw: Dict) -> Dict:
    return {
        "max_silence_seconds": _positive_int(
            raw.get("max_silence_seconds"), "health.max_silence_seconds"),
        "max_event_lag_seconds": _positive_int(
            raw.get("max_event_lag_seconds"), "health.max_event_lag_seconds"),
    }


def _parse_storage(raw: Dict) -> Dict:
    path = raw.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"storage.path 必须是绝对路径，实际: {path!r}")
    return {
        "path": path,
        "busy_timeout_ms": _positive_int(raw.get("busy_timeout_ms"), "storage.busy_timeout_ms"),
        "sent_retention_days": _positive_int(
            raw.get("sent_retention_days"), "storage.sent_retention_days"),
        "audit_retention_days": _positive_int(
            raw.get("audit_retention_days"), "storage.audit_retention_days"),
        "max_bytes": _positive_int(raw.get("max_bytes"), "storage.max_bytes"),
    }


def _parse_transport(raw: Dict) -> Dict:
    initial = _positive(raw.get("reconnect_initial_seconds"),
                       "transport.reconnect_initial_seconds")
    maximum = _positive(raw.get("reconnect_max_seconds"),
                       "transport.reconnect_max_seconds")
    if maximum < initial:
        raise ValueError("transport.reconnect_max_seconds 不得小于初始值")
    return {
        "heartbeat_seconds": _positive_int(
            raw.get("heartbeat_seconds"), "transport.heartbeat_seconds"),
        "reconnect_initial_seconds": initial,
        "reconnect_max_seconds": maximum,
        "refill_requests_per_second": _positive(
            raw.get("refill_requests_per_second"),
            "transport.refill_requests_per_second"),
    }


def _parse_reference_sync(raw: Dict) -> Dict:
    heartbeat = _positive_int(raw.get("heartbeat_seconds"),
                              "reference_sync.heartbeat_seconds")
    silence = _positive_int(raw.get("max_silence_seconds"),
                            "reference_sync.max_silence_seconds")
    if silence < 3 * heartbeat:
        raise ValueError("reference_sync.max_silence_seconds 必须 ≥ 3 倍 heartbeat_seconds")
    return {"heartbeat_seconds": heartbeat, "max_silence_seconds": silence}


def _parse_delivery(raw: Dict) -> Dict:
    initial = _positive(raw.get("retry_initial_seconds"),
                        "delivery.retry_initial_seconds")
    maximum = _positive(raw.get("retry_max_seconds"),
                        "delivery.retry_max_seconds")
    if maximum < initial:
        raise ValueError("delivery.retry_max_seconds 不得小于初始值")
    return {
        "queue_capacity": _positive_int(raw.get("queue_capacity"),
                                       "delivery.queue_capacity"),
        "retry_initial_seconds": initial,
        "retry_max_seconds": maximum,
        "event_ttl_seconds": _positive_int(raw.get("event_ttl_seconds"),
                                           "delivery.event_ttl_seconds"),
        "report_delay_seconds": _positive_int(
            raw.get("report_delay_seconds", 30),
            "delivery.report_delay_seconds"),
        "maintenance_interval_seconds": _positive_int(
            raw.get("maintenance_interval_seconds", 60),
            "delivery.maintenance_interval_seconds"),
    }


def _parse_shadow_research(raw: Optional[Dict], urgent_repeat_s: int) -> Dict:
    data = raw or {"enabled": False}
    interval = _positive_int(data.get("notice_interval_seconds", 1800),
                            "shadow_research.notice_interval_seconds")
    if interval < urgent_repeat_s:
        raise ValueError("shadow_research.notice_interval_seconds 须 ≥ repeat.urgent_seconds")
    if not isinstance(data.get("enabled", False), bool):
        raise ValueError("shadow_research.enabled 必须是布尔值")
    return {"enabled": data.get("enabled", False), "notice_interval_seconds": interval}


def _cross_check(cfg: Dict) -> None:
    """跨字段约束（§9.2 校验清单）。"""
    normal, urgent = cfg["normal"], cfg["urgent"]
    recovery, repeat = cfg["recovery"], cfg["repeat"]
    if urgent["near_grid_fraction"] > normal["near_grid_fraction"]:
        raise ValueError("urgent.near_grid_fraction 不得大于 normal.near_grid_fraction")
    if normal["near_grid_fraction"] >= recovery["inside_fraction"]:
        raise ValueError("normal.near_grid_fraction 必须小于 recovery.inside_fraction")
    if recovery["inside_fraction"] >= Decimal("0.5"):
        raise ValueError("recovery.inside_fraction 必须小于 0.5")
    if urgent["min_efficiency"] < normal["min_efficiency"]:
        raise ValueError("urgent.min_efficiency 不得低于 normal.min_efficiency")
    if urgent["return_3m"] < normal["return_3m"] or urgent["return_5m"] < normal["return_5m"]:
        raise ValueError("紧急涨跌幅门槛不得低于普通门槛")
    if urgent["hold_seconds"] > normal["hold_seconds"]:
        raise ValueError("urgent.hold_seconds 不得大于 normal.hold_seconds")
    if repeat["boundary_seconds"] > repeat["urgent_seconds"]:
        raise ValueError("repeat.boundary_seconds 不得大于 urgent_seconds")
    storage, sync = cfg["storage"], cfg["reference_sync"]
    busy_s = storage["busy_timeout_ms"] / MS_PER_SECOND
    if busy_s >= sync["max_silence_seconds"]:
        raise ValueError("storage.busy_timeout_ms/1000 必须小于 reference_sync.max_silence_seconds")


_SECTION_PARSERS = {
    "reference": lambda raw: {"max_age_seconds": _positive_int(
        raw.get("max_age_seconds"), "reference.max_age_seconds")},
    "features": _parse_features,
    "normal": _parse_normal,
    "urgent": _parse_urgent,
    "recovery": _parse_recovery,
    "repeat": _parse_repeat,
    "health": _parse_health,
    "storage": _parse_storage,
    "transport": _parse_transport,
    "reference_sync": _parse_reference_sync,
    "delivery": _parse_delivery,
}


def parse_profile(raw: Dict) -> Dict:
    """解析并校验一份 realtime_alert profile（§9.1/§9.2）。

    mode=alert 时拒绝研究字段 e_resample_seconds（≠1）；缺必填节/字段即抛
    ValueError，不回退硬编码默认。
    """
    mode = raw.get("mode")
    if mode not in ("shadow", "alert"):
        raise ValueError(f"mode 必须是 shadow/alert，实际: {mode!r}")
    if not isinstance(raw.get("enabled", False), bool):
        raise ValueError("enabled 必须是布尔值")
    symbol = raw.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol 必须为非空字符串")
    if not raw.get("price_source"):
        raise ValueError("price_source 不能为空")
    unknown = set(raw) - set(_REQUIRED_SECTIONS) - {
        "enabled", "mode", "profile", "symbol", "price_source",
        "efficiency_filter", "shadow_research"}
    if unknown:
        raise ValueError(f"未知配置节/字段: {sorted(unknown)}")
    missing = [name for name in _REQUIRED_SECTIONS if name not in raw]
    if missing:
        raise ValueError(f"缺少必填配置节: {missing}")
    cfg: Dict = {
        "enabled": raw["enabled"], "mode": mode, "symbol": symbol,
        "price_source": raw["price_source"],
        "profile": raw.get("profile"),
    }
    for name, parser in _SECTION_PARSERS.items():
        cfg[name] = parser(raw[name])
    cfg["efficiency_filter"] = {"enabled": raw.get(
        "efficiency_filter", {"enabled": True})["enabled"]}
    if not isinstance(cfg["efficiency_filter"]["enabled"], bool):
        raise ValueError("efficiency_filter.enabled 必须是布尔值")
    cfg["shadow_research"] = _parse_shadow_research(
        raw.get("shadow_research"), cfg["repeat"]["urgent_seconds"])
    if mode == "alert" and cfg["features"]["e_resample_seconds"] != 1:
        raise ValueError("alert 模式不得使用研究字段 features.e_resample_seconds")
    _cross_check(cfg)
    return cfg
