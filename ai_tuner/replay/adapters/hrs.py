"""HRS 回放适配器：历史时点状态恢复 + 数据缺口 fail-closed。

契约边界（Spec F6）：
- 三张独立状态表 ``hrs.hrs_positions`` / ``hrs.hrs_blacklist`` /
  ``hrs.hrs_active_symbols`` 必须按 ``as_of`` 历史时点恢复，缺任一张即 ``DataBlocked``；
- 每张表的值必须是**带时点与完整性证明的历史快照记录**（键固定为
  ``SNAPSHOT_REQUIRED_KEYS``），不是「当前表的行列表」；空行列表 ``[]`` 只能来自
  「已成功采集且确实为空」的历史快照，禁止用当前表零行推断历史时点为空；
- K 线缓存要求 ``1h`` / ``4h`` 两个周期，任一 symbol 的空覆盖同样 ``DataBlocked``；
- 本适配器只 import 标准库与 ``ai_tuner.replay`` 内部模块，不 import
  ``strategies.hrs.strategy``（会拖入数据库/网络依赖），也不 import ``backtest.*``；
- 不读取系统时钟或当前时间，不回填实时数据，不 fallback 到当前快照；
- 决策编排（信号编排 / 目标价）在 S6 接入：接线前 ``intents()`` 一律抛
  ``QualificationBlocked``，任何 ``run_replay`` 都会在首根 K 线失败，
  因此不会产出「零交易的伪成功」回放结果。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..broker import PortfolioView
from ..models import Bar, DataBlocked, QualificationBlocked, parse_utc
from .base import PreparedContext


#: 每张状态表的历史快照记录必须齐备的键（缺任一即 BLOCKED_DATA）
SNAPSHOT_REQUIRED_KEYS = ("as_of", "complete", "rows")

StateLoader = Callable[[datetime], Mapping[str, Optional[Mapping[str, Any]]]]


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
        """注入「按历史时点恢复三张表快照记录」的查询。

        Args:
            state_loader: ``state_loader(as_of)`` 返回「表名 → 历史快照记录」的映射，
                每张表的值为 ``None``（该表在 ``as_of`` 时点不可得）或形如
                ``{"as_of": <datetime 或 ISO 字符串>, "complete": True/False,
                "rows": [...]}`` 的**历史快照记录**。

                约定：``rows == []`` 只有在 ``complete is True`` 且快照 ``as_of``
                与回放时点一致时合法——代表「该时点确证为空」；禁止用当前表零行
                推断历史时点为空。
        """
        self._state_loader = state_loader
        self._state: Mapping[str, Optional[Mapping[str, Any]]] = {}
        self._latest_bars: Dict[str, Bar] = {}
        self._latest_snapshot: Optional[Any] = None

    def prepare(self, portal: Any) -> PreparedContext:
        """按 ``portal.as_of`` 恢复历史状态并校验 K 线覆盖；缺任一项抛 DataBlocked。"""
        as_of = portal.as_of
        raw_state = self._state_loader(as_of)
        universe = portal.universe()

        portal_as_of = parse_utc(as_of)
        gaps: List[str] = []
        state: Dict[str, Mapping[str, Any]] = {}
        for name in self.REQUIRED_STATE_TABLES:
            snapshot, table_gaps = _validate_snapshot(
                name, raw_state.get(name), portal_as_of
            )
            gaps.extend(table_gaps)
            if snapshot is not None:
                state[name] = snapshot

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
        """产出意图：决策尚未接线，**无条件**抛 ``QualificationBlocked``。

        S6 接线时整体替换本方法体（实现真实决策编排并返回意图）。在此之前
        ``run_replay`` 会在首根 K 线即失败，绝不返回「零交易但成功」的结果。
        """
        raise QualificationBlocked(["hrs_decision_not_wired"])

    def protective_prices(self, symbol: str) -> Optional[dict]:
        """返回 None：HRS 目标价在 S6 接入，本 Spec 不伪造止损/止盈价格。

        该返回值只在**无持仓**时才合法；有持仓仍返回 None 属于未接线，由
        ``intents`` 的显式阻断保证本适配器不会被误用（接线前回放必然在首根
        K 线就抛 ``QualificationBlocked``）。
        """
        return None


def _validate_snapshot(
    table: str, raw: Any, portal_as_of: datetime
) -> Tuple[Optional[Mapping[str, Any]], List[str]]:
    """校验单表历史快照记录，返回（通过校验的快照, gap 列表）。

    gap 码口径：``missing_at_as_of`` / ``invalid_snapshot`` / ``incomplete_at_as_of`` /
    ``as_of_missing`` / ``as_of_invalid`` / ``as_of_mismatch`` / ``rows_missing``。
    ``rows == []`` 在 ``complete is True`` 且 ``as_of`` 一致时合法，不产生 gap。
    """
    if raw is None:
        return None, ["{0}:missing_at_as_of".format(table)]
    if not isinstance(raw, Mapping):
        return None, ["{0}:invalid_snapshot".format(table)]

    gaps: List[str] = []
    if "as_of" not in raw:
        gaps.append("{0}:as_of_missing".format(table))
    else:
        snapshot_as_of = _parse_as_of(raw.get("as_of"))
        if snapshot_as_of is None:
            gaps.append("{0}:as_of_invalid".format(table))
        elif snapshot_as_of != portal_as_of:
            gaps.append("{0}:as_of_mismatch".format(table))

    if raw.get("complete") is not True:
        gaps.append("{0}:incomplete_at_as_of".format(table))

    rows = raw.get("rows")
    if "rows" not in raw or not isinstance(rows, (list, tuple)):
        gaps.append("{0}:rows_missing".format(table))

    if gaps:
        return None, gaps
    return raw, gaps


def _parse_as_of(value: Any) -> Optional[datetime]:
    """把快照 ``as_of`` 统一转 aware UTC datetime；无法解析返回 None（不读时钟）。"""
    if not isinstance(value, (datetime, str)):
        return None
    try:
        return parse_utc(value)
    except (TypeError, ValueError):
        return None
