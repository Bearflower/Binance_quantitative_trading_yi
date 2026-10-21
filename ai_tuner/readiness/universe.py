# -*- coding: utf-8 -*-
"""
策略 universe 解析契约（阶段二 Task 2）

两类来源：
- strategy_config：静态 universe，从策略生效配置的点号路径读取 symbol 列表
  （btc_eth_aggressive 即用 strategy.symbols）；配置文件原始字节计算 sha256 哈希，
  审计可复现——防止配置后来修改后重解释历史结果
- historical_snapshot：动态 universe，从 append-only 历史表 public.ai_tuner_universe_snapshots
  查询 observed_at <= as_of 的快照；空历史必须返回 gap，**严禁**回退到
  public.strategy_states 当前值

禁止硬编码 symbol / 周期 / 阈值——全部从配置读取。
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from . import db_probe


# ---------------------------------------------------------------------------
# 不可变模型（frozen + tuple 容器，审计可哈希、可比较）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UniverseSlice:
    """单一 universe 快照在指定时间窗口内的生效切片

    一个 strategy_id 在一次审计窗口内可能覆盖多个 slice；静态策略固定产出
    1 个 slice，历史策略按每次 observed_at 快照形成非重叠 slice。
    """
    strategy_id: str
    valid_from: datetime
    valid_to: datetime
    symbols: tuple[str, ...]
    intervals: tuple[str, ...]
    source: str             # "strategy_config" | "historical_snapshot"
    config_hash: str        # 64 字符 16 进制 sha256，审计可复现


@dataclass(frozen=True)
class UniverseResolution:
    """resolve() 的完整返回值"""
    slices: tuple[UniverseSlice, ...]
    covered_from: Optional[datetime]  # 最早 observed_at（历史型）或 None（静态型不适用）
    gaps: tuple[str, ...]             # 审计 gap 文案，空历史时以 historical_universe_missing 开头


# ---------------------------------------------------------------------------
# 模块级工具函数
# ---------------------------------------------------------------------------


def _resolve_dot_path(data: Any, dot_path: str) -> Any:
    """按点号路径在嵌套 dict 上逐层下钻，返回叶子值

    不存在的中间键 / 叶子键 → KeyError（明确抛出，不静默）。
    用于把 config.yaml 的 "strategy.symbols" 这样的路径解析到实际 list。
    """
    keys = dot_path.split(".")
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            raise KeyError(f"点号路径 '{dot_path}' 中 '{key}' 不存在")
        cur = cur[key]
    return cur


def _compute_config_hash(raw_bytes: bytes) -> str:
    """对配置文件原始字节计算 sha256，返回 16 进制 64 字符串"""
    return hashlib.sha256(raw_bytes).hexdigest()


def _coerce_tuple(value: Any) -> tuple[str, ...]:
    """把 list / 其他可迭代容器强制转成 tuple[str, ...]（不可变 + 类型收敛）"""
    if value is None:
        return ()
    return tuple(str(v) for v in value)


# ---------------------------------------------------------------------------
# 顶层路由：UniverseResolver
# ---------------------------------------------------------------------------

# source 字段允许值（防御性白名单，未知值 → ValueError）
_VALID_SOURCE = {"strategy_config", "historical_snapshot"}


class UniverseResolver:
    """顶层路由，按 config 中的 source 字段分发到具体 resolver

    构造参数：
      universe_cfg: purpose_driven_mvp.readiness.universe 段的完整 dict
        {strategy_id: {"source": "...", ...per-strategy...}, ...}
      project_root: 仓库根（把 config_path 拼成绝对路径）
      db: 异步 DB 接口（仅 historical_snapshot 型需要，strategy_config 可传 None）
    """

    def __init__(
        self,
        universe_cfg: dict[str, dict[str, Any]],
        project_root: Path,
        db: Optional[Any] = None,
    ) -> None:
        self._cfg = dict(universe_cfg)
        self._root = Path(project_root)
        self._db = db

    async def resolve(
        self,
        strategy_id: str,
        valid_from: datetime,
        valid_to: datetime,
    ) -> UniverseResolution:
        """解析指定策略在窗口 [valid_from, valid_to] 内的 universe"""
        entry = self._cfg.get(strategy_id)
        if entry is None:
            raise ValueError(
                f"universe 配置中未知的 strategy_id='{strategy_id}'，"
                f"当前已声明: {list(self._cfg.keys())}"
            )
        source = entry.get("source")
        if source not in _VALID_SOURCE:
            raise ValueError(
                f"universe.{strategy_id}.source 非法: {source!r}，"
                f"允许值: {sorted(_VALID_SOURCE)}"
            )

        if source == "strategy_config":
            return await StaticConfigUniverseResolver(
                entry=entry, project_root=self._root
            ).resolve(strategy_id, valid_from, valid_to)

        # historical_snapshot
        if self._db is None:
            raise ValueError(
                f"strategy '{strategy_id}' 的 universe.source=historical_snapshot，"
                f"但 UniverseResolver 未注入 db"
            )
        return await HistoricalSnapshotUniverseResolver(
            entry=entry, db=self._db
        ).resolve(strategy_id, valid_from, valid_to)


# ---------------------------------------------------------------------------
# 子 resolver：strategy_config
# ---------------------------------------------------------------------------


class StaticConfigUniverseResolver:
    """从策略生效配置文件读取静态 universe（btc_eth_aggressive 等）

    不依赖 DB——纯文件读取 + 哈希计算。
    """

    def __init__(self, entry: dict[str, Any], project_root: Path) -> None:
        self._config_path = entry.get("config_path")
        self._symbol_path = entry.get("symbol_path", "strategy.symbols")
        self._intervals = _coerce_tuple(entry.get("intervals", []))
        self._root = Path(project_root)
        if not self._config_path:
            raise ValueError(
                "strategy_config 源缺少 config_path 字段"
            )

    async def resolve(
        self,
        strategy_id: str,
        valid_from: datetime,
        valid_to: datetime,
    ) -> UniverseResolution:
        raw = self._read_config_bytes()
        config_hash = _compute_config_hash(raw)
        data = yaml.safe_load(raw) or {}
        symbols = _coerce_tuple(_resolve_dot_path(data, self._symbol_path))
        slice_ = UniverseSlice(
            strategy_id=strategy_id,
            valid_from=valid_from,
            valid_to=valid_to,
            symbols=symbols,
            intervals=self._intervals,
            source="strategy_config",
            config_hash=config_hash,
        )
        # 静态型 covered_from 无意义（配置文件无时间戳）→ 用 None 避免误导
        return UniverseResolution(
            slices=(slice_,),
            covered_from=None,
            gaps=(),
        )

    def _read_config_bytes(self) -> bytes:
        """读取配置文件原始字节（绝对路径）；不存在 → FileNotFoundError"""
        path = self._root / self._config_path
        if not path.is_file():
            raise FileNotFoundError(
                f"universe config_path 指向的文件不存在: {path}"
            )
        return path.read_bytes()


# ---------------------------------------------------------------------------
# 子 resolver：historical_snapshot
# ---------------------------------------------------------------------------


# 查询模板：只用 observed_at <= $1 单条件，
# 禁止任何对 public.strategy_states 的回退查询（计划 §4.1 强制）
_SNAPSHOT_SQL = """
SELECT observed_at, symbol, config_hash
FROM public.ai_tuner_universe_snapshots
WHERE strategy_id = $1
  AND observed_at <= $2
ORDER BY observed_at ASC, symbol ASC
"""


class HistoricalSnapshotUniverseResolver:
    """从 append-only 历史表查询动态 universe（HRS 等）

    只查 observed_at <= as_of 的历史快照；空历史时返回 gap 而不是静默空。
    严禁回退到 public.strategy_states 当前值——历史口径必须来自历史。
    """

    def __init__(self, entry: dict[str, Any], db: Any) -> None:
        self._intervals = _coerce_tuple(entry.get("intervals", []))
        self._db = db

    async def resolve(
        self,
        strategy_id: str,
        valid_from: datetime,
        valid_to: datetime,
    ) -> UniverseResolution:
        # 时间列类型探测：observed_at 按列类型做 naive/aware 绑定
        observed_bound = await self._bind_observed_at(valid_to)

        rows = await self._db.fetch_all(
            _SNAPSHOT_SQL,
            strategy_id,
            observed_bound,
        )
        if not rows:
            # 空历史：严禁回退 public.strategy_states；gap 文案带 before=<timestamp>
            before_ts = self._fmt_boundary(observed_bound)
            gap = (
                f"historical_universe_missing_before={before_ts},"
                f" strategy_id={strategy_id}"
            )
            return self._empty_resolution(gap)

        groups = self._group_snapshots(rows)
        earliest = groups[0][0]
        slices = self._build_slices(strategy_id, groups, valid_from, valid_to)
        gaps: tuple[str, ...] = ()
        if earliest > self._as_utc(valid_from):
            gaps = (
                f"historical_universe_missing_before={earliest.isoformat()},"
                f" strategy_id={strategy_id}",
            )
        return UniverseResolution(
            slices=slices,
            covered_from=earliest,
            gaps=gaps,
        )

    def _empty_resolution(self, gap: str) -> UniverseResolution:
        """空历史早退：返回带单 gap 的空 UniverseResolution"""
        return UniverseResolution(slices=(), covered_from=None, gaps=(gap,))

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _group_snapshots(
        self, rows: list,
    ) -> list[tuple[datetime, tuple[str, ...], str]]:
        """把同一 observed_at 的行还原为一次完整 universe 快照。"""
        groups: list[tuple[datetime, tuple[str, ...], str]] = []
        current_at: Optional[datetime] = None
        current_symbols: list[str] = []
        current_hash = ""
        for row in rows:
            observed_at = self._as_utc(row["observed_at"])
            if current_at is not None and observed_at != current_at:
                groups.append((current_at, tuple(current_symbols), current_hash))
                current_symbols = []
            if current_at != observed_at:
                current_at = observed_at
                current_hash = str(row["config_hash"] or "")
            current_symbols.append(str(row["symbol"]))
        if current_at is not None:
            groups.append((current_at, tuple(current_symbols), current_hash))
        return groups

    def _build_slices(
        self,
        strategy_id: str,
        groups: list[tuple[datetime, tuple[str, ...], str]],
        valid_from: datetime,
        valid_to: datetime,
    ) -> tuple[UniverseSlice, ...]:
        """按快照生效时间构建与审计窗口相交的非重叠切片。"""
        window_start = self._as_utc(valid_from)
        window_end = self._as_utc(valid_to)
        baseline = None
        changes: list[tuple[datetime, tuple[str, ...], str]] = []
        for group in groups:
            if group[0] <= window_start:
                baseline = group
            elif group[0] < window_end:
                changes.append(group)

        effective = ([baseline] if baseline is not None else []) + changes
        slices: list[UniverseSlice] = []
        for index, group in enumerate(effective):
            observed_at, symbols, config_hash = group
            start = window_start if baseline is group else observed_at
            end = effective[index + 1][0] if index + 1 < len(effective) else window_end
            if start >= end:
                continue
            slices.append(UniverseSlice(
                strategy_id=strategy_id,
                valid_from=start,
                valid_to=end,
                symbols=symbols,
                intervals=self._intervals,
                source="historical_snapshot",
                config_hash=config_hash,
            ))
        return tuple(slices)

    async def _bind_observed_at(self, as_of: datetime) -> datetime:
        """探测 observed_at 列类型并把 as_of 正确绑定（委托 db_probe）"""
        return await db_probe.bind_datetime(
            self._db, "public", "ai_tuner_universe_snapshots", "observed_at", as_of,
        )

    @staticmethod
    def _fmt_boundary(dt: datetime) -> str:
        """把边界时间格式化成 ISO 字符串（含 tz，gap 文案可审计定位）"""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
