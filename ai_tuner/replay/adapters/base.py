"""适配器协议：把策略纯计算接到统一事件循环上。

约定（契约 §6）：
- 适配器不得持有 Broker 引用、不得直接撮合、不得读系统时钟；
- 缺数据抛 ``DataBlocked``（结构化 gaps），由 engine 转 BLOCKED_DATA；
- MTPCS 只允许 import ``strategies/btc_eth_aggressive/core``，不得 import ``strategy.py``；
- HRS 只调现有 ``strategies/hrs/*``，缺历史数据即 BLOCKED_DATA。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol, Tuple

from ..broker import PortfolioView
from ..models import (
    Bar,
    ConfigHashes,
    DataBlocked,
    ExternalSnapshot,
    OrderIntent,
    UniverseSlice,
)

__all__ = [
    "DataBlocked",
    "PreparedContext",
    "ReplayAdapter",
]


@dataclass
class PreparedContext:
    """适配器准备阶段产出的只读上下文（portfolio 由 engine 每根 K 线刷新）。"""

    strategy_id: str
    as_of: datetime
    universe: UniverseSlice
    config_hashes: ConfigHashes
    portfolio: PortfolioView
    extras: dict = field(default_factory=dict)


class ReplayAdapter(Protocol):
    """回放适配器协议。"""

    strategy_id: str

    #: 是否依赖外部时点快照（MTPCS=False，HRS=True）
    requires_external_snapshot: bool

    def prepare(self, data: Any) -> PreparedContext:
        """一次性加载 as_of 前的 universe/配置指纹；缺数据抛 DataBlocked。"""
        ...

    def on_bar(self, bar: Bar, ctx: PreparedContext) -> None:
        """消费一根已收盘 K 线，更新内部指标状态（不得读取未来信息）。"""
        ...

    def on_snapshot(self, snap: ExternalSnapshot, ctx: PreparedContext) -> None:
        """消费一个外部时点快照（MTPCS 恒为空实现）。"""
        ...

    def intents(self, ctx: PreparedContext) -> Tuple[OrderIntent, ...]:
        """从当前指标状态产出意图（确定性序）。"""
        ...

    def protective_prices(self, position: Any) -> Optional[dict]:
        """持仓开仓后返回止损/止盈价格（由 core/exits 计算）。"""
        ...
