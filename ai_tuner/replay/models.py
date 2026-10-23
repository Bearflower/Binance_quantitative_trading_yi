"""回放内核的冻结数据模型与框架异常。

约定（契约 §2）：
- 全部 dataclass 冻结（frozen=True），跨模块传递只读；
- 金额/价格/数量一律 ``decimal.Decimal``，禁止 float 参与撮合与盈亏；
- 时间一律 aware UTC datetime；
- 本模块不做任何 IO，不 import 策略代码，不 import ``backtest.*``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

UTC = timezone.utc

ZERO = Decimal("0")


# ===========================================================================
# 框架异常（契约 §3 / §6）
# ===========================================================================


class ReplayError(RuntimeError):
    """回放框架异常基类。"""


class FutureDataAccess(ReplayError):
    """越界访问未来数据（框架级违规，不得捕获降级）。"""

    def __init__(
        self,
        interface: str,
        as_of: datetime,
        offending_field: str,
        offending_value: Any,
    ) -> None:
        self.interface = interface
        self.as_of = as_of
        self.offending_field = offending_field
        self.offending_value = offending_value
        super().__init__(
            "检测到未来数据访问：interface={0} as_of={1} field={2} value={3}".format(
                interface, to_utc_z(as_of), offending_field, offending_value
            )
        )

    def to_payload(self) -> Dict[str, Any]:
        """转成 audit_events.jsonl 的 payload。"""
        return {
            "interface": self.interface,
            "as_of": to_utc_z(self.as_of),
            "offending_field": self.offending_field,
            "offending_value": str(self.offending_value),
        }


class DataBlocked(ReplayError):
    """必需数据缺失或覆盖度不足（fail-closed，归 BLOCKED_DATA）。"""

    def __init__(self, gaps: Any) -> None:
        self.gaps = tuple(gaps)
        super().__init__("数据不完整（fail-closed）：" + ", ".join(self.gaps))


class QualificationBlocked(ReplayError):
    """样本量/校准项未达标，资格考试被阻断（归 BLOCKED_DATA，不判 FAIL）。"""

    def __init__(self, reasons: Any) -> None:
        self.reasons = tuple(reasons)
        super().__init__("资格考试被阻断：" + ", ".join(self.reasons))


# ===========================================================================
# 时间与数值工具（无副作用，供全包共用）
# ===========================================================================


def to_utc_z(value: datetime) -> str:
    """datetime → UTC ISO 8601 且以 Z 结尾（chrono 口径，写入报告）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(raw: Any) -> datetime:
    """解析 ISO 8601 / Z 后缀 / 毫秒时间戳 → aware UTC datetime。"""
    if isinstance(raw, datetime):
        return raw.astimezone(UTC) if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(Decimal(str(raw)) / Decimal("1000"), tz=UTC)
    text = str(raw)
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def dec(raw: Any) -> Decimal:
    """安全转 Decimal（None → 0），禁止经由 float 中转。"""
    if raw is None:
        return ZERO
    if isinstance(raw, Decimal):
        return raw
    return Decimal(str(raw))


# ===========================================================================
# 数据模型
# ===========================================================================


@dataclass(frozen=True)
class Bar:
    """一根已收盘 K 线。"""

    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades: int

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("Bar.symbol 不能为空")
        if not self.interval:
            raise ValueError("Bar.interval 不能为空")
        if self.high < self.low:
            raise ValueError("Bar.high 必须 >= Bar.low")
        if not (self.low <= self.open <= self.high):
            raise ValueError("Bar.open 必须落在 [low, high] 内")
        if not (self.low <= self.close <= self.high):
            raise ValueError("Bar.close 必须落在 [low, high] 内")
        if self.volume < ZERO:
            raise ValueError("Bar.volume 必须 >= 0")
        if self.quote_volume < ZERO:
            raise ValueError("Bar.quote_volume 必须 >= 0")
        if self.trades < 0:
            raise ValueError("Bar.trades 必须 >= 0")


@dataclass(frozen=True)
class UniverseSlice:
    """某段时间内的可交易集合（顺序即遍历顺序，写入快照即冻结）。"""

    strategy_id: str
    valid_from: datetime
    valid_to: datetime
    symbols: Tuple[str, ...]
    intervals: Tuple[str, ...]
    source: str
    config_hash: str

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise ValueError("UniverseSlice.strategy_id 不能为空")
        if self.valid_from >= self.valid_to:
            raise ValueError("UniverseSlice.valid_from 必须早于 valid_to")
        if not self.symbols:
            raise ValueError("UniverseSlice.symbols 不能为空")
        if not self.intervals:
            raise ValueError("UniverseSlice.intervals 不能为空")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("UniverseSlice.symbols 必须去重")
        if len(set(self.intervals)) != len(self.intervals):
            raise ValueError("UniverseSlice.intervals 必须去重")
        if len(self.config_hash) != 64:
            raise ValueError("UniverseSlice.config_hash 必须是 64 位 SHA-256 hex")


@dataclass(frozen=True)
class ExternalSnapshot:
    """HRS 外部时点快照（对齐 public.ai_tuner_market_snapshots）。"""

    strategy_id: str
    symbol: str
    observed_at: datetime
    bucket_start: datetime
    open_interest_usd: Optional[Decimal]
    funding_rate: Optional[Decimal]
    market_cap_usd: Optional[Decimal]
    oi_market_cap_ratio: Optional[Decimal]
    sentiment_score: Optional[Decimal]
    oi_source: str
    funding_source: str
    market_cap_source: str
    payload_hash: str
    is_complete: bool


@dataclass(frozen=True)
class ConfigHashes:
    """某个 as_of 时点生效的配置身份（基础配置 / 参数 manifest / 覆盖层）。"""

    strategy_id: str
    as_of: datetime
    base_config_hash: str
    params_manifest_hash: str
    overlay_hash: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "base_config_hash": self.base_config_hash,
            "params_manifest_hash": self.params_manifest_hash,
            "overlay_hash": self.overlay_hash,
        }


SIDES = ("LONG", "SHORT")
ORDER_TYPES = ("MARKET", "LIMIT", "STOP", "TAKE_PROFIT")
EXIT_REASONS = (
    "STOP_LOSS",
    "TP1",
    "TP2",
    "TRAILING_STOP",
    "TIME_STOP",
    "REDUCE_ONLY",
)

# 同根 K 线退出判定固定优先级（契约 §4.3：命中即停，数字小者优先）。
EXIT_PRIORITY = {
    "STOP_LOSS": 1,
    "TP1": 2,
    "TP2": 3,
    "TRAILING_STOP": 4,
    "TIME_STOP": 5,
    "REDUCE_ONLY": 6,
}


@dataclass(frozen=True)
class OrderIntent:
    """适配器产出的「想做什么」。由 Broker 负责撮合，适配器不得自行撮合。"""

    strategy_id: str
    symbol: str
    seq: int
    side: str
    order_type: str
    quantity: Decimal
    reason: str
    trigger_price: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None
    reduce_only: bool = False
    close_ratio: Decimal = Decimal("1")
    grade: Optional[str] = None
    reference_price: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if self.side not in SIDES:
            raise ValueError("OrderIntent.side 非法：{0}".format(self.side))
        if self.order_type not in ORDER_TYPES:
            raise ValueError("OrderIntent.order_type 非法：{0}".format(self.order_type))
        if not self.reason:
            raise ValueError("OrderIntent.reason 不能为空")
        if self.quantity <= ZERO:
            raise ValueError("OrderIntent.quantity 必须 > 0")
        if not (ZERO < self.close_ratio <= Decimal("1")):
            raise ValueError("OrderIntent.close_ratio 必须落在 (0, 1]")
        if self.order_type in ("STOP", "TAKE_PROFIT", "LIMIT") and self.trigger_price is None:
            raise ValueError(
                "OrderIntent.order_type={0} 必须提供 trigger_price".format(self.order_type)
            )


@dataclass(frozen=True)
class Order:
    """Broker 内部订单（Intent + 生命周期）。"""

    order_id: str
    intent: OrderIntent
    status: str
    created_at: datetime


@dataclass(frozen=True)
class Fill:
    """一笔成交。fee / slippage_cost 为负（支出）。"""

    order_id: str
    fill_price: Decimal
    quantity: Decimal
    fee: Decimal
    slippage_cost: Decimal
    ts: datetime


@dataclass(frozen=True)
class Position:
    """当前持仓（部分平仓后 current_quantity 递减）。"""

    position_id: str
    symbol: str
    direction: str
    entry_price: Decimal
    entry_quantity: Decimal
    current_quantity: Decimal
    atr: Decimal
    grade: str
    entry_time: datetime
    stop_loss_price: Optional[Decimal] = None
    tp1_price: Optional[Decimal] = None
    tp2_price: Optional[Decimal] = None
    trailing_stop_price: Optional[Decimal] = None
    trailing_activated: bool = False
    tp1_hit: bool = False
    tp2_hit: bool = False

    def is_open(self) -> bool:
        return self.current_quantity > ZERO


@dataclass(frozen=True)
class ClosedTrade:
    """已平仓（含部分平仓）批次。net_pnl 已含费用、滑点与资金费。"""

    trade_id: str
    position_id: str
    symbol: str
    direction: str
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    realized_pnl: Decimal
    fee_paid: Decimal
    slippage_cost: Decimal
    funding_fee: Decimal
    net_pnl: Decimal
    exit_reason: str
    entry_time: datetime
    exit_time: datetime
    mae: Decimal
    mfe: Decimal


@dataclass(frozen=True)
class EquityPoint:
    """盯市曲线点；最大回撤以 marked_equity 为准。"""

    ts: datetime
    realized_equity: Decimal
    marked_equity: Decimal
    has_open_position: bool


@dataclass(frozen=True)
class ReplayResult:
    """一次回放的完整结果。"""

    manifest_ref: str
    equity_curve: Tuple[EquityPoint, ...]
    closed_trades: Tuple[ClosedTrade, ...]
    final_position: Optional[Position]
    audit_events: Tuple[Dict[str, Any], ...]
    invariant_violations: Tuple[str, ...]
