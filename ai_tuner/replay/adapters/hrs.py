"""HRS 回放适配器：历史时点状态恢复 + 数据缺口 fail-closed。

契约边界（Spec F6）：
- 三张独立状态表 ``hrs.hrs_positions`` / ``hrs.hrs_blacklist`` /
  ``hrs.hrs_active_symbols`` 必须按 ``as_of`` 历史时点恢复，缺任一张即 ``DataBlocked``；
- K 线缓存要求 ``1h`` / ``4h`` 两个周期，任一 symbol 的空覆盖同样 ``DataBlocked``；
- 本适配器只 import 标准库与 ``ai_tuner.replay`` 内部模块，不 import
  ``strategies.hrs.strategy``（会拖入数据库/网络依赖），也不 import ``backtest.*``；
- 不读取系统时钟或当前时间，不回填实时数据，不 fallback 到当前快照；
- 决策编排（信号编排 / 目标价）在 S6 接入，本 Spec 只保证历史时点恢复与
  BLOCKED_DATA 语义，不得伪造信号或保护价。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..broker import PortfolioView
from ..models import Bar, DataBlocked
from .base import PreparedContext


StateLoader = Callable[[datetime], Mapping[str, Optional[Sequence[Mapping[str, Any]]]]]


class HrsAdapter:
    """HRS 回放适配器（仅历史时点恢复，不含决策编排）。"""

    strategy_id = "hrs"

    #: HRS 依赖外部时点快照（market_snapshots），回放事件流需纳入 snapshot 事件
    requires_external_snapshot = True

    #: 必须按 as_of 时点恢复的独立状态表（缺任一张即 BLOCKED_DATA）
    REQUIRED_STATE_TABLES = ("hrs_positions", "hrs_blacklist", "hrs_active_symbols")

    #: 必须齐备的 K 线周期（HRS 决策同时依赖 1h 与 4h 缓存）
    REQUIRED_INTERVALS = ("1h", "4h")

    def __init__(self, state_loader: StateLoader) -> None:
        """注入「按历史时点恢复三张表」的查询。

        Args:
            state_loader: ``state_loader(as_of)`` 返回形如
                ``{"hrs_positions": [...], "hrs_blacklist": [...],
                "hrs_active_symbols": [...]}`` 的映射（后续由只读查询实现）。

                约定：某个 key 的值为 ``None`` 表示该表在 ``as_of`` 时点
                **不可得**（历史上没有留存该表的快照），而不是「空表」；
                空列表 ``[]`` 是合法值，表示该时点确证为空表，不得阻断回放。
        """
        self._state_loader = state_loader
        self._state: Mapping[str, Optional[Sequence[Mapping[str, Any]]]] = {}
        self._latest_bars: Dict[str, Bar] = {}
        self._latest_snapshot: Optional[Any] = None

    def prepare(self, portal: Any) -> PreparedContext:
        """按 ``portal.as_of`` 恢复历史状态并校验 K 线覆盖；缺任一项抛 DataBlocked。"""
        as_of = portal.as_of
        state = self._state_loader(as_of)
        universe = portal.universe()

        gaps: List[str] = []
        for name in self.REQUIRED_STATE_TABLES:
            if state.get(name) is None:
                gaps.append("{0}:missing_at_as_of".format(name))

        for symbol in universe.symbols:
            for interval in self.REQUIRED_INTERVALS:
                if not self._has_bars(portal, symbol, interval):
                    gaps.append("kline_empty:{0}:{1}".format(symbol, interval))

        if gaps:
            raise DataBlocked(gaps)

        config_hashes = portal.config_version()
        self._state = state
        return PreparedContext(
            strategy_id=self.strategy_id,
            as_of=as_of,
            universe=universe,
            config_hashes=config_hashes,
            portfolio=PortfolioView(
                positions=(),
                last_prices={},
                realized_equity=Decimal("0"),
            ),
            extras={"symbols": list(universe.symbols), "state": state},
        )

    @staticmethod
    def _has_bars(portal: Any, symbol: str, interval: str) -> bool:
        """该 symbol/interval 在 as_of 前是否有已收盘 K 线（缺失文件视同空覆盖）。"""
        try:
            bars = portal.bars(symbol, interval)
        except DataBlocked:
            return False
        return bool(bars)

    def on_bar(self, bar: Bar, ctx: PreparedContext) -> None:
        """只记录该 symbol/interval 的最新收盘 K 线到内存（不写盘、不补值）。"""
        key = "{0}|{1}".format(bar.symbol, bar.interval)
        previous = self._latest_bars.get(key)
        if previous is None or bar.close_time >= previous.close_time:
            self._latest_bars[key] = bar

    def on_snapshot(self, snap: Any, ctx: PreparedContext) -> None:
        """保存最新外部时点快照引用（HRS 依赖外部数据），不做任何补值。"""
        self._latest_snapshot = snap

    def intents(self, ctx: PreparedContext) -> Tuple[Any, ...]:
        """返回空意图：HRS 决策编排在 S6 接入，本 Spec 只保证恢复与 BLOCKED_DATA 语义。"""
        return ()

    def protective_prices(self, symbol: str) -> Optional[dict]:
        """返回 None：HRS 目标价在 S6 接入，本 Spec 不伪造止损/止盈价格。"""
        return None
