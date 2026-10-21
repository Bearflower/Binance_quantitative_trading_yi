# -*- coding: utf-8 -*-
"""
阶段二 Task 3：逐 symbol K 线覆盖审计

把"主力合约抽样"升级为"策略 universe 逐 symbol、按应有 bar 数加权"。

核心口径（计划 §4.2）：
- expected(symbol, interval) = 完整闭合时间桶数量（window_start 向上对齐、
  window_end 向下对齐到 interval 边界）
- actual(symbol, interval)   = generate_series 左连接实际表的 COUNT(DISTINCT open_time)
- coverage(symbol, interval) = min(actual / expected, 1.0)
- weighted_coverage          = sum(actual) / sum(expected)

约束：
- 尚未闭合的最后一根 K 线不计入分母或分子
- 重复行不提高覆盖（靠 COUNT(DISTINCT open_time)）
- 缺表按 0%，不能从其他 symbol 或其他周期替代
- universe 未配置 → weighted 0.0 + gap，不抛异常
- HRS 历史 universe 缺失 → weighted 0.0 + historical_universe_missing，不查询 K 线表

SQL 唯一模板：
  SELECT COUNT(*) AS expected,
         COUNT(DISTINCT k.open_time) AS actual
  FROM generate_series($1::timestamp, $2::timestamp, $3::interval) AS expected(open_time)
  LEFT JOIN {schema}.{table} k ON k.open_time = expected.open_time
动态部分只有已通过 guard 的 {table}（schema 固定 btc_eth），$1/$2/$3 走参数绑定。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Iterable, Optional, Tuple

import structlog

from . import db_probe

logger = structlog.get_logger()


# ===========================================================================
# interval → 秒（协议常量，属于数据口径协议，不是业务阈值）
# ===========================================================================
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# interval 白名单正则（与 INTERVAL_SECONDS 键集合严格一致）
_INTERVAL_RE = re.compile(r"^(1m|5m|15m|1h|4h|1d)$")

# symbol 白名单正则（币安合约 symbol 统一格式：大写字母+数字，2-20 字符）
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")

# 最终表名正则（防拼写出裸 f-string：kline_xxx_1h 等）
_TABLE_RE = re.compile(r"^kline_[a-z0-9]{2,20}_(1m|5m|15m|1h|4h|1d)$")

# K 线表固定 schema（所有生产 K 线在 btc_eth schema）
_KLINE_SCHEMA = "btc_eth"

# 时间列候选名（固定优先级）
_TS_CANDIDATES = ("open_time", "timestamp", "ts")

# 保留兼容参数；缺口必须按真实连续性合并，不能为压缩报告伪造连续区间
_MAX_MISSING_RANGES = 200


# ===========================================================================
# 不可变模型
# ===========================================================================


@dataclass(frozen=True)
class SymbolCoverage:
    """单个 symbol/interval 的覆盖度"""
    symbol: str
    interval: str
    expected: int
    actual: int
    missing_ranges: tuple[tuple[datetime, datetime], ...] = ()
    gap: Optional[str] = None

    @property
    def coverage(self) -> float:
        return min(self.actual / self.expected, 1.0) if self.expected else 0.0


@dataclass(frozen=True)
class KlineCoverageResult:
    weighted_coverage: float
    symbols: tuple[SymbolCoverage, ...]
    evidence_start: Optional[datetime]
    evidence_end: Optional[datetime]
    gaps: tuple[str, ...]


# ===========================================================================
# 聚合
# ===========================================================================


def aggregate_coverage(rows: Iterable[SymbolCoverage]) -> KlineCoverageResult:
    """把逐 symbol/interval 结果聚合成加权覆盖

    weighted_coverage = sum(actual) / sum(expected)；sum(expected)==0 → 0.0。
    """
    rows = list(rows)
    total_expected = sum(r.expected for r in rows)
    total_actual = sum(r.actual for r in rows)
    weighted = total_actual / total_expected if total_expected > 0 else 0.0

    gaps: list[str] = []
    for r in rows:
        if r.gap:
            gaps.append(f"{r.symbol}/{r.interval}: {r.gap}")

    evidence_start = None
    evidence_end = None
    for r in rows:
        for s, e in r.missing_ranges:
            if evidence_start is None or s < evidence_start:
                evidence_start = s
            if evidence_end is None or e > evidence_end:
                evidence_end = e

    return KlineCoverageResult(
        weighted_coverage=weighted,
        symbols=tuple(rows),
        evidence_start=evidence_start,
        evidence_end=evidence_end,
        gaps=tuple(gaps),
    )


# ===========================================================================
# 窗口闭合对齐
# ===========================================================================


def _period_seconds(interval: str) -> Optional[int]:
    """interval → 秒；未知 interval → None（记录 gap，不炸）"""
    return INTERVAL_SECONDS.get(interval)


def _align_window(window_start: datetime, window_end: datetime, period_sec: int
                  ) -> Tuple[datetime, datetime]:
    """闭合 bar 对齐：start 向上取整，end 向下取整

    start 落在桶中间 → 归入下一桶；end 落在桶中间 → 不计入那根未闭合 bar。
    interval 视为"UTC epoch 对齐"（1h/4h/1d 自然日 0 点对齐）。
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    # 先统一到 epoch-aware UTC 再取整，避免 naive/aware 混用
    if window_start.tzinfo is None:
        ws = window_start.replace(tzinfo=timezone.utc)
    else:
        ws = window_start.astimezone(timezone.utc)
    if window_end.tzinfo is None:
        we = window_end.replace(tzinfo=timezone.utc)
    else:
        we = window_end.astimezone(timezone.utc)

    ws_seconds = (ws - epoch).total_seconds()
    we_seconds = (we - epoch).total_seconds()

    # 向上取整 start（ceil）
    ws_aligned = epoch + timedelta(
        seconds=(int(ws_seconds // period_sec) + (1 if ws_seconds % period_sec else 0)) * period_sec
    )
    # 向下取整 end（floor）
    we_aligned = epoch + timedelta(
        seconds=int(we_seconds // period_sec) * period_sec
    )
    # 转回 naive（postgresql open_time 是 naive TIMESTAMP）
    return ws_aligned.replace(tzinfo=None), we_aligned.replace(tzinfo=None)


# ===========================================================================
# 表名校验（fail-closed，禁止内联拼裸表名）
# ===========================================================================


def _build_table_name(symbol: str, interval: str) -> str:
    """严格白名单校验后组装 K 线表名

    与计划 §3.3 复用 services.kline_service.core.table_name_guard.build_table_name_by_format
    的偏差：ai_tuner/Dockerfile 不 COPY services/ 包，且 table_name_guard 内部依赖
    shared.utils.logger（shared/utils.py 是单文件模块不是包，运行时 ImportError）。
    为保持同等 fail-closed 不变量，在 ai_tuner 内用正则白名单校验，
    禁止内联 f"kline_{symbol.lower()}_{interval}" 裸拼接。

    Args:
        symbol:   原始 symbol（可能含小写/空格，先 .strip().upper() 规整）
        interval: interval 字符串（必须 ∈ INTERVAL_SECONDS）

    Returns:
        通过三重校验的表名（如 "kline_btcusdt_1h"）

    Raises:
        ValueError: interval 未知 / symbol 格式非法 / 最终表名正则校验失败
    """
    # interval 白名单校验（禁止任何未知 interval 进入后续流程）
    if not _INTERVAL_RE.fullmatch(str(interval)):
        raise ValueError(f"未知 interval: {interval!r}，必须 ∈ {list(INTERVAL_SECONDS)}")

    # symbol 规整 + 白名单
    symbol_clean = str(symbol).strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol_clean):
        raise ValueError(f"symbol 格式非法: {symbol!r}")

    # 组装后必须通过最终表名正则（防御：防手滑拼出 kline_BTCUSDT_1h 这种）
    table = f"kline_{symbol_clean.lower()}_{interval}"
    if not _TABLE_RE.fullmatch(table):
        raise ValueError(f"最终表名格式非法: {table!r}")

    return table


# ===========================================================================
# 时间列探测（委托 db_probe，消除重复代码）
# ===========================================================================


async def _detect_ts_column(db, schema: str, table: str) -> Tuple[Optional[str], Optional[str]]:
    """探测 K 线表时间列名与列类型（open_time → timestamp → ts）

    Returns:
        (列名, data_type)；均为 None 表示未找到
    """
    for cand in _TS_CANDIDATES:
        dtype = await db_probe.fetch_column_data_type(db, schema, table, cand)
        if dtype is not None:
            return cand, dtype
    return None, None


def _boundary_cast(dtype: Optional[str]) -> str:
    """根据列 data_type 决定 generate_series 窗口边界的 SQL cast 字面量

    PostgreSQL 的 generate_series(timestamptz, interval) 会受会话 TZ 影响。
    若 K 线表 open_time 是 timestamptz（带时区）而窗口边界硬写成 ::timestamp（naive），
    容器 TZ=Asia/Shanghai 会让窗口边界整体偏移 8 小时 → 覆盖率错算。
    因此按列类型动态选择 cast，确保窗口边界与实际表列类型一致。

    仅返回 "timestamptz" 或 "timestamp" 两个字面量，安全注入 SQL 模板。
    """
    if dtype == db_probe.TS_AWARE:
        return "timestamptz"
    return "timestamp"


# ===========================================================================
# SQL：count + missing
# ===========================================================================


_COUNT_SQL = """
SELECT COUNT(*) AS expected,
       COUNT(DISTINCT k.{ts_col}) AS actual
FROM generate_series($1::{boundary_cast}, $2::{boundary_cast} - $3::interval, $3::interval) AS expected(open_time)
LEFT JOIN {schema_table} k ON k.{ts_col} = expected.open_time
"""

_MISSING_SQL = """
SELECT g.open_time FROM generate_series($1::{boundary_cast}, $2::{boundary_cast} - $3::interval, $3::interval) g(open_time)
LEFT JOIN {schema_table} k ON k.{ts_col} = g.open_time
WHERE k.{ts_col} IS NULL
ORDER BY g.open_time
"""


async def _query_count(db, schema: str, table: str, ts_col: str,
                       start: datetime, end: datetime, period_sec: int,
                       boundary_cast: str = "timestamp",
                       ) -> Tuple[int, int, Optional[str]]:
    """generate_series 计数：(expected, actual, gap_or_None)

    异常 / row is None → expected 用 _expected_buckets 兜底、actual=0，
    并产出 gap=f"count_query_failed:{exc[:120]}" 供外层 SymbolCoverage 透传。
    不再静默记 0（fail-open 掩盖故障 → 审计无法区分"真的没 K 线"与"查询炸了"）。

    Args:
        boundary_cast: generate_series 窗口边界的 SQL cast 字面量
                       （"timestamp" / "timestamptz"，由 _boundary_cast 推导）。
    """
    sql = _COUNT_SQL.format(
        schema_table=f"{schema}.{table}", ts_col=ts_col, boundary_cast=boundary_cast,
    )
    start_bound = await db_probe.bind_datetime(db, schema, table, ts_col, start)
    end_bound = await db_probe.bind_datetime(db, schema, table, ts_col, end)
    # asyncpg 的 interval 编码器接收 datetime.timedelta；字符串只适用于 SQL
    # 字面量，作为绑定参数会触发 "str object has no attribute days"。
    interval_delta = timedelta(seconds=period_sec)
    try:
        row = await db.fetch_one(sql, start_bound, end_bound, interval_delta)
    except Exception as exc:
        expected = _expected_buckets(start, end, period_sec)
        truncated = str(exc)[:120]
        logger.warning("K 线 generate_series 计数失败", schema=schema, table=table, error=truncated)
        return expected, 0, f"count_query_failed:{truncated}"
    if row is None:
        expected = _expected_buckets(start, end, period_sec)
        return expected, 0, "count_query_row_none"
    return int(row.get("expected") or 0), int(row.get("actual") or 0), None


async def _query_missing_starts(db, schema: str, table: str, ts_col: str,
                                start: datetime, end: datetime, period_sec: int,
                                boundary_cast: str = "timestamp",
                                ) -> tuple[tuple[datetime, ...], Optional[str]]:
    """返回 (缺失桶的 open_time 序列, gap_or_None)

    异常时 gap = f"missing_query_failed:{exc[:120]}"，starts = ()——
    调用方必须把 gap 合并进 SymbolCoverage.gap，不能静默显示"无缺失区间"。
    """
    sql = _MISSING_SQL.format(
        schema_table=f"{schema}.{table}", ts_col=ts_col, boundary_cast=boundary_cast,
    )
    start_bound = await db_probe.bind_datetime(db, schema, table, ts_col, start)
    end_bound = await db_probe.bind_datetime(db, schema, table, ts_col, end)
    interval_delta = timedelta(seconds=period_sec)
    try:
        rows = await db.fetch_all(sql, start_bound, end_bound, interval_delta)
    except Exception as exc:
        truncated = str(exc)[:120]
        logger.warning("K 线缺失桶查询失败", schema=schema, table=table, error=truncated)
        return (), f"missing_query_failed:{truncated}"
    return tuple(r["open_time"] for r in rows), None


# ===========================================================================
# 缺失区间合并
# ===========================================================================


def _merge_missing_starts(
    starts: Iterable[datetime],
    *,
    interval: str,
    max_ranges: int = _MAX_MISSING_RANGES,
) -> tuple[tuple[datetime, datetime], ...]:
    """把连续缺失桶的 open_time 序列合并成 (start, end) 区间（左闭右开）

    从 INTERVAL_SECONDS 模块常量取 interval 对应秒数，不再从调用方传 periods——
    单一可信源，消除幽灵形参 + 避免两处不一致（audit 里改 INTERVAL_SECONDS 忘记改调用）。
    """
    period = INTERVAL_SECONDS.get(interval)
    if period is None or not starts:
        return ()
    starts_sorted = sorted(starts)
    _ = max_ranges  # API 兼容；正确性优先，不按数量伪合并离散缺口

    ranges: list[tuple[datetime, datetime]] = []
    seg_start = starts_sorted[0]
    seg_prev = seg_start
    for cur in starts_sorted[1:]:
        if (cur - seg_prev).total_seconds() == period:
            seg_prev = cur
        else:
            ranges.append((seg_start, seg_prev + timedelta(seconds=period)))
            seg_start = cur
            seg_prev = cur
    ranges.append((seg_start, seg_prev + timedelta(seconds=period)))
    return tuple(ranges)


# ===========================================================================
# 逐 symbol/interval 查询单 pair
# ===========================================================================


def _placeholder_row(symbol: str, interval: str, expected: int,
                     gap: Optional[str] = None, actual: int = 0,
                     missing_ranges: tuple = ()) -> SymbolCoverage:
    """统一构造 SymbolCoverage 占位结果（缺表/无 ts 列/计数失败早退分支复用）"""
    return SymbolCoverage(
        symbol=symbol, interval=interval,
        expected=expected, actual=actual,
        missing_ranges=missing_ranges, gap=gap,
    )


def _merge_gaps(*gaps: Optional[str]) -> Optional[str]:
    """把多个可选 gap 串合并：取非 None 项用 "; " 连接，全部 None → None"""
    present = [g for g in gaps if g]
    if not present:
        return None
    return "; ".join(present)


async def _pair_counts(db, table: str, ts_col: str, period_sec: int,
                       ws: datetime, we: datetime, bc: str,
                       symbol: str, interval: str) -> SymbolCoverage:
    """执行 count + missing 查询 + gap 合并，返回完整 SymbolCoverage"""
    expected, actual, count_gap = await _query_count(
        db, _KLINE_SCHEMA, table, ts_col, ws, we, period_sec, boundary_cast=bc,
    )
    if count_gap is not None:
        # 计数 SQL 异常 → gap 透传，expected/actual 已在 _query_count 内兜底
        return _placeholder_row(symbol, interval, expected, count_gap, actual, ())
    if expected == 0:
        return _placeholder_row(symbol, interval, 0, None, 0, ())
    missing_starts, missing_gap = await _query_missing_starts(
        db, _KLINE_SCHEMA, table, ts_col, ws, we, period_sec, boundary_cast=bc,
    )
    missing_ranges = _merge_missing_starts(missing_starts, interval=interval)
    final_gap = _merge_gaps(count_gap, missing_gap)
    return _placeholder_row(
        symbol, interval, expected, final_gap, actual, missing_ranges,
    )


async def _single_pair_coverage(db,
                                symbol: str, interval: str,
                                window_start: datetime, window_end: datetime,
                                ) -> SymbolCoverage:
    """计算单个 (symbol, interval) 的覆盖；失败时返回 gap=... 的占位结果"""
    period_sec = _period_seconds(interval)
    if period_sec is None:
        return _placeholder_row(symbol, interval, 0, f"unknown_interval:{interval}")

    # 表名本地白名单校验（fail-closed，禁止 ImportError fallback 内联拼裸表名）
    # 原因：ai_tuner/Dockerfile 不 COPY services/，
    # 且 services.kline_service.core 依赖 shared.utils.logger（单文件模块不是包）
    # → 运行时 ImportError，为保持同等不变量在 ai_tuner 内实现
    try:
        validated_table = _build_table_name(symbol, interval)
    except Exception as exc:
        logger.info("K 线表名校验失败，按缺表计", symbol=symbol, interval=interval, error=str(exc))
        expected = _expected_buckets(window_start, window_end, period_sec)
        return _placeholder_row(symbol, interval, expected, "table_name_format_invalid")

    table_state = await db_probe.table_exists(db, _KLINE_SCHEMA, validated_table)
    if table_state is None:
        expected = _expected_buckets(window_start, window_end, period_sec)
        return _placeholder_row(symbol, interval, expected, "table_probe_failed")

    # 查询成功且确定表不存在 → expected 仍按应有桶数计，actual=0
    if table_state is False:
        ws, we = _align_window(window_start, window_end, period_sec)
        expected = max(0, int((we - ws).total_seconds() / period_sec))
        # 缺表等价于完整闭合窗口全部缺失，可安全生成一个精确补采区间。
        # 与查询失败不同：这里不是把未知状态猜成缺口，而是已确认存储表不存在。
        missing_ranges = ((ws, we),) if expected > 0 else ()
        return _placeholder_row(
            symbol, interval, expected, "table_missing", 0, missing_ranges,
        )

    # 表存在 → 探测时间列 + 计算
    ts_col, ts_dtype = await _detect_ts_column(db, _KLINE_SCHEMA, validated_table)
    if ts_col is None:
        expected = _expected_buckets(window_start, window_end, period_sec)
        return _placeholder_row(symbol, interval, expected, "timestamp_column_missing")

    # 根据列类型决定 generate_series 窗口边界的 cast，避免会话 TZ 偏移
    bc = _boundary_cast(ts_dtype)
    ws, we = _align_window(window_start, window_end, period_sec)
    return await _pair_counts(db, validated_table, ts_col, period_sec,
                              ws, we, bc, symbol, interval)


def _expected_buckets(window_start: datetime, window_end: datetime, period_sec: int) -> int:
    """按闭合对齐后计算 expected bar 数"""
    ws, we = _align_window(window_start, window_end, period_sec)
    total = int((we - ws).total_seconds() / period_sec)
    return max(0, total)


# ===========================================================================
# 主入口：compute_kline_coverage
# ===========================================================================


def _empty_result(gap) -> KlineCoverageResult:
    """构造零覆盖空结果（universe 未配置 / resolver 异常 / 空历史 3 个早退复用）"""
    gaps = (gap,) if isinstance(gap, str) else tuple(gap)
    return KlineCoverageResult(
        weighted_coverage=0.0, symbols=(),
        evidence_start=None, evidence_end=None,
        gaps=gaps,
    )


def _iter_pairs(resolution) -> Iterable[tuple[Any, str, str]]:
    """从 UniverseResolution 生成 (slice, symbol, interval) 序列。"""
    for sl in resolution.slices:
        for symbol in sl.symbols:
            for interval in sl.intervals:
                yield sl, symbol, interval


def _as_utc(value: datetime) -> datetime:
    """把 naive/aware datetime 统一为 UTC-aware，供边界比较使用。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _combine_symbol_rows(rows: Iterable[SymbolCoverage]) -> list[SymbolCoverage]:
    """把同一 symbol/interval 的非重叠 universe 切片汇总为一行。"""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        bucket = grouped.setdefault(
            (row.symbol, row.interval),
            {"expected": 0, "actual": 0, "ranges": [], "gaps": []},
        )
        bucket["expected"] += row.expected
        bucket["actual"] += row.actual
        bucket["ranges"].extend(row.missing_ranges)
        if row.gap and row.gap not in bucket["gaps"]:
            bucket["gaps"].append(row.gap)

    combined: list[SymbolCoverage] = []
    for (symbol, interval), bucket in grouped.items():
        combined.append(SymbolCoverage(
            symbol=symbol,
            interval=interval,
            expected=bucket["expected"],
            actual=bucket["actual"],
            missing_ranges=tuple(sorted(bucket["ranges"])),
            gap="; ".join(bucket["gaps"]) or None,
        ))
    return combined


async def compute_kline_coverage(
    db,
    *,
    strategy_id: str,
    universe_cfg: Optional[dict[str, Any]],
    project_root: Path,
    window_start: datetime,
    window_end: datetime,
) -> KlineCoverageResult:
    """对指定策略的 universe 逐 symbol/interval 计算加权 K 线覆盖

    Args:
        db:            异步 DB 接口（fetch_one / fetch_all）
        strategy_id:   候选策略 ID
        universe_cfg:  readiness.universe 段完整 dict；None → 视为未配置
        project_root:  仓库根（给 universe resolver 用）
        window_start:  审计窗口起点（datetime，tz-aware 或 naive 均可）
        window_end:    审计窗口终点

    异常策略：
        - universe_cfg 里没有 strategy_id → 返回 weighted 0.0 + gap，**不抛异常**
        - universe resolver 报错 → 吞异常，gap 里说明
    """
    if not universe_cfg or strategy_id not in universe_cfg:
        return _empty_result(f"universe_not_configured_for_{strategy_id}")

    try:
        from .universe import UniverseResolver
        resolver = UniverseResolver(universe_cfg, project_root=Path(project_root), db=db)
        resolution = await resolver.resolve(strategy_id, window_start, window_end)
    except Exception as exc:
        logger.warning("universe resolver 失败，K 线覆盖记 0",
                       strategy=strategy_id, error=str(exc))
        return _empty_result(f"universe_resolver_failed:{exc}")

    # 历史 universe 只要未覆盖完整审计窗口就 fail-closed；不能用局部切片的
    # 高覆盖率掩盖更早时期 universe 不可证明的问题。
    historical_gap = any(
        str(gap).startswith("historical_universe_missing")
        for gap in resolution.gaps
    )
    if not resolution.slices or historical_gap:
        return _empty_result(resolution.gaps)

    rows: list[SymbolCoverage] = []
    audit_start = _as_utc(window_start)
    audit_end = _as_utc(window_end)
    for sl, symbol, interval in _iter_pairs(resolution):
        slice_start = max(audit_start, _as_utc(sl.valid_from))
        slice_end = min(audit_end, _as_utc(sl.valid_to))
        if slice_start >= slice_end:
            continue
        pair = await _single_pair_coverage(
            db, symbol, interval,
            window_start=slice_start, window_end=slice_end,
        )
        rows.append(pair)
    aggregated = aggregate_coverage(_combine_symbol_rows(rows))
    return KlineCoverageResult(
        weighted_coverage=aggregated.weighted_coverage,
        symbols=aggregated.symbols,
        evidence_start=aggregated.evidence_start,
        evidence_end=aggregated.evidence_end,
        gaps=tuple(resolution.gaps) + aggregated.gaps,
    )


# ===========================================================================
# 结果转 JSON 安全字典（供 auditor._audit_single 塞入 CandidateReadiness.kline_symbols）
# ===========================================================================


def symbol_row_to_dict(row: SymbolCoverage) -> dict:
    """SymbolCoverage → JSON 安全 dict（datetime 一律 isoformat 字符串）"""
    missing_ranges = []
    for (s, e) in row.missing_ranges:
        s_iso = s.isoformat() if isinstance(s, datetime) else str(s)
        e_iso = e.isoformat() if isinstance(e, datetime) else str(e)
        missing_ranges.append([s_iso, e_iso])
    return {
        "symbol": row.symbol,
        "interval": row.interval,
        "expected": row.expected,
        "actual": row.actual,
        "coverage": row.coverage,
        "missing_ranges": missing_ranges,
        "gap": row.gap,
    }
