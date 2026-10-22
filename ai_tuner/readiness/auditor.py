"""
90 天准备度审计器

对每个 MVP 候选策略查询 PostgreSQL，产出：
- 已平仓交易数、有效信号数、市场状态覆盖度
- 交易记录字段完整度、配置身份可追溯度、K 线覆盖度、外部快照完整度
- 各项硬门禁是否通过（阈值全部可配置，严禁硬编码）

关键设计：
- K 线 schema 不硬编码 public.，而是通过 information_schema 动态探测
- 信号表 schema 直接等于 strategy_id（信号按 schema 隔离），表名用 ILIKE '%signal%' 探测
- 时间列类型探测：所有窗口查询先查 information_schema.columns 判断 tz-aware，
  naive 列去 tzinfo 绑定，aware 列保留 UTC——解决 asyncpg naive/aware 类型冲突
- K 线覆盖度改为按"主力合约"取表（该策略在窗口内 symbol 出现次数最多者），
  避免任意取第一张 K 线表（经常取到无关小币）
- 外部快照表不存在时 coverage=0 并在 gaps 列表里说明（预期行为）
- 配置身份覆盖度：从 config purpose_driven_mvp.identity_ratio_baseline 读取（强制键）
- 策略选择优先序：读 config candidates 顺序
- min_external_snapshot_coverage 无兜底，由 config 决定
- regime_count = COUNT(DISTINCT symbol) 是 proxy，非真实市场状态数
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog

from . import db_probe
from .models import CandidateReadiness, ReadinessResult

logger = structlog.get_logger()


@dataclass(frozen=True)
class SnapshotCoverage:
    expected: int
    complete: int
    coverage: float
    evidence_start: Optional[datetime]
    sources: List[str]
    gaps: List[str]


def format_ratio(value: float) -> str:
    """把 0-1 浮点比率格式化为百分比字符串（保留两位小数）。

    门禁 failure/gaps 文案和 CLI 摘要必须复用同一精度，避免
    97.96% 因 .1% 四舍五入成 98.0% 跟阈值 98.0% 看起来自相矛盾。
    """
    return f"{value:.2%}"


# ---------- 模块常量 ----------

# 信号表候选时间列名（按固定优先级匹配）
_SIGNAL_TS_CANDIDATES = ["created_at", "signal_time", "triggered_at", "executed_at"]

# 信号表候选表名后缀（按优先级匹配，覆盖 trade_signals / hrs_signals / short_signals）
_SIGNAL_TABLE_PATTERN = "%signal%"

# status 串允许字符白名单（防 SQL 注入：auditor 用 f-string 直接拼进 AND status IN (...)）
# 仅允许 [A-Za-z0-9_]——PostgreSQL status 列一般都是 snake_case
_ALLOWED_STATUS_CHAR = re.compile(r"^[A-Za-z0-9_]+$")


# ---------- 策略级有效信号状态集合校验 ----------


def _validate_effective_signal_statuses(raw: Any) -> Dict[str, List[str]]:
    """校验 readiness.effective_signal_statuses 配置值

    可选键，若为 None 或空 dict → 返回 {}（所有策略走纯 COUNT(*)）。
    若存在，必须满足：
      - 整体类型是 dict
      - 每个 value 是非空 list[str]
      - 每个 str 只含 [A-Za-z0-9_]（防 SQL 注入）
    任一违反 → ValueError（构造阶段即炸，不静默）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"purpose_driven_mvp.readiness.effective_signal_statuses 必须是 dict，"
            f"实际: {type(raw).__name__}"
        )
    validated: Dict[str, List[str]] = {}
    for sid, statuses in raw.items():
        if not isinstance(statuses, list) or len(statuses) == 0:
            raise ValueError(
                f"effective_signal_statuses.{sid} 必须是非空 list，"
                f"实际: {statuses!r}"
            )
        clean: List[str] = []
        for s in statuses:
            if not isinstance(s, str) or not _ALLOWED_STATUS_CHAR.match(s):
                raise ValueError(
                    f"effective_signal_statuses.{sid} 中含非法 status 串: {s!r} "
                    f"（仅允许 [A-Za-z0-9_]）"
                )
            clean.append(s)
        validated[str(sid).strip()] = clean
    return validated


# ---------- 配置强制键清单 ----------

# readiness 段必须存在的键（缺失即 ValueError，不静默）
# 含 window_days（不再 get 兜底）
_REQUIRED_READINESS_KEYS = [
    "window_days",
    "min_closed_trades",
    "min_effective_signals",
    "min_trade_field_completeness",
    "min_config_identity_coverage",
    "min_kline_coverage",
    "min_external_snapshot_coverage",
    "min_regime_count",
    "external_snapshot_requirement",
]

# mvp 顶层必须存在的键
_REQUIRED_MVP_KEYS = [
    "candidates",
    "identity_ratio_baseline",  # 强制键，不再 get 兜底
]

# per_strategy 每个策略条目必须含的键（缺失即 ValueError，不静默）
_REQUIRED_PER_STRATEGY_KEYS = [
    "min_external_snapshot_coverage",
    "external_snapshot_requirement",
]


def _init_from_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """从 config.yaml 解析 auditor 所需的全部配置属性（含强制键校验）

    返回 dict 含 candidates / identity_baseline / readiness_cfg / per_strategy /
    effective_signal_statuses / universe_cfg——__init__ 只需逐一赋值。
    """
    mvp = config.get("purpose_driven_mvp", {})
    missing_mvp = [k for k in _REQUIRED_MVP_KEYS if k not in mvp]
    if missing_mvp:
        raise ValueError(f"purpose_driven_mvp 缺键: {missing_mvp}")

    readiness_cfg = mvp.get("readiness", {})
    missing = [k for k in _REQUIRED_READINESS_KEYS if k not in readiness_cfg]
    if missing:
        raise ValueError(f"purpose_driven_mvp.readiness 缺键: {missing}")

    per_strategy: Dict[str, dict] = {}
    per_cfg = readiness_cfg.get("per_strategy", {}) or {}
    for sid, entry in per_cfg.items():
        missing_keys = [k for k in _REQUIRED_PER_STRATEGY_KEYS if k not in entry]
        if missing_keys:
            raise ValueError(
                f"purpose_driven_mvp.readiness.per_strategy.{sid} 缺键: {missing_keys}"
            )
        per_strategy[sid] = dict(entry)

    return {
        "candidates": list(mvp["candidates"]),
        "identity_baseline": float(mvp["identity_ratio_baseline"]),
        "readiness_cfg": readiness_cfg,
        "per_strategy": per_strategy,
        "effective_signal_statuses": _validate_effective_signal_statuses(
            readiness_cfg.get("effective_signal_statuses")
        ),
        "universe_cfg": dict(readiness_cfg.get("universe") or {}),
    }


class ReadinessAuditor:
    """执行 90 天准备度审计"""

    def __init__(
        self,
        db_manager,
        config: Dict[str, Any],
        project_root: Optional[Path] = None,
    ):
        """
        Args:
            db_manager:   DatabaseManager 实例（异步）
            config:       完整 ai_tuner/config.yaml 内容
            project_root: 仓库根（给 universe resolver / table_name_guard 用）。
                          默认 Path(__file__).resolve().parents[2]，现有调用点
                          （scripts/ai_tuner_mvp.py、测试 fixture）都无需改动即可工作。
        """
        self.db = db_manager
        # 仓库根：默认按文件位置推断，允许显式传入（测试可覆盖）
        self.project_root = Path(
            project_root if project_root is not None
            else Path(__file__).resolve().parents[2]
        )
        attrs = _init_from_config(config)
        self.candidates = attrs["candidates"]
        self.identity_baseline = attrs["identity_baseline"]
        self.readiness_cfg = attrs["readiness_cfg"]
        self.per_strategy = attrs["per_strategy"]
        self.effective_signal_statuses = attrs["effective_signal_statuses"]
        self.universe_cfg = attrs["universe_cfg"]

    # ---------------------------------------------------------------
    # 辅助：schema / 表 / 列类型探测 + 时间绑定 + 公共窗口计数
    # ---------------------------------------------------------------

    async def _fetch_column_data_type(
        self, schema: str, table: str, column: str
    ) -> Optional[str]:
        """委托 db_probe：消除与 kline_coverage.py 的重复探测逻辑"""
        return await db_probe.fetch_column_data_type(self.db, schema, table, column)

    async def _bind_window(
        self, schema: str, table: str, column: str, dt: datetime
    ) -> datetime:
        """委托 db_probe：按时间列类型把 dt 正确绑定（消除与 kline_coverage.py 的重复）"""
        return await db_probe.bind_datetime(self.db, schema, table, column, dt)

    async def _count_rows_in_window(
        self,
        schema: str, table: str, ts_col: str,
        window_start: datetime, window_end: datetime,
        extra_condition: str = "",
    ) -> int:
        """公共窗口计数：
        COUNT(*) AS cnt FROM {schema}.{table}
        WHERE {ts_col} >= $1 AND {ts_col} < $2 {extra_condition}

        两个调用方（信号计数 / K 线覆盖）共享的精确同构 SQL 块，消除重复。
        时间绑定走 _bind_window（按列类型自动转换 naive/aware）。

        extra_condition 为可选的额外 WHERE 子句（注意调用方须带前导空格，
        如 " AND status IN ('pending','executed')"），默认空串时 K 线等调用方行为不变。
        此参数**不得引入新的 $N 占位符**，避免打乱 $1/$2 的参数序号。
        """
        sql = f"""
        SELECT COUNT(*) AS cnt
        FROM {schema}.{table}
        WHERE {ts_col} >= $1
          AND {ts_col} <  $2
          {extra_condition}
        """
        ws = await self._bind_window(schema, table, ts_col, window_start)
        we = await self._bind_window(schema, table, ts_col, window_end)
        row = await self.db.fetch_one(sql, ws, we)
        return int((row or {}).get("cnt") or 0)

    async def _schema_exists(self, schema_name: str) -> bool:
        """验证指定 schema 是否存在于 information_schema.schemata"""
        sql = """
        SELECT 1 FROM information_schema.schemata WHERE schema_name = $1
        """
        try:
            row = await self.db.fetch_one(sql, schema_name)
            return row is not None
        except Exception as exc:
            logger.warning("schema 存在性探测失败", schema=schema_name, error=str(exc))
            return False

    async def _find_signal_table(self, schema_name: str) -> Optional[str]:
        """在指定 schema 下按 ILIKE '%signal%' 找信号表，按表名排序取第一个"""
        sql = f"""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = '{schema_name}' AND table_name ILIKE $1
        ORDER BY table_name LIMIT 1
        """
        try:
            row = await self.db.fetch_one(sql, _SIGNAL_TABLE_PATTERN)
            return row["table_name"] if row else None
        except Exception as exc:
            logger.warning("信号表探测失败", schema=schema_name, error=str(exc))
            return None

    async def _detect_signal_timestamp_column(
        self, schema_name: str, table_name: str
    ) -> Optional[str]:
        """在信号表候选时间列集合中按固定优先级找第一个存在的列

        只返回列名（is_aware 由调用方通过 _fetch_column_data_type 单独判断，
        避免在本方法内重复查询 data_type）。
        """
        for cand in _SIGNAL_TS_CANDIDATES:
            dtype = await self._fetch_column_data_type(schema_name, table_name, cand)
            if dtype is not None:
                return cand
        logger.warning(
            "信号表无可用时间戳列",
            schema=schema_name, table=table_name,
            candidates=_SIGNAL_TS_CANDIDATES,
        )
        return None

    # ---------------------------------------------------------------
    # 信号表探测 + 计数（拆成 probe / count 两步，各 ≤50 行）
    # ---------------------------------------------------------------

    async def _probe_signal_table_and_col(
        self, strategy_id: str
    ) -> Tuple[Optional[str], Optional[str], List[str]]:
        """探测策略信号表 + 时间列（独立方法，让 _query_signal_count 主体 ≤50 行）

        Returns:
            (table_name_or_None, ts_col_or_None, gaps_for_missing_cases)
            成功 → (table_name, ts_col, [])；失败 → (None, None, gaps)
        """
        # Step 1: schema 验证（schema 直接是 strategy_id）
        if not await self._schema_exists(strategy_id):
            return None, None, [f"schema {strategy_id} 不存在，effective_signals 记 0"]

        # Step 2: 信号表探测（ILIKE '%signal%'，覆盖 trade_signals/hrs_signals/short_signals）
        table_name = await self._find_signal_table(strategy_id)
        if table_name is None:
            return None, None, [
                f"{strategy_id} schema 下无信号表（table_name ILIKE '%signal%'），"
                f"effective_signals 记 0"
            ]

        # Step 3: 时间列探测（候选集合固定优先级）
        ts_col = await self._detect_signal_timestamp_column(strategy_id, table_name)
        if ts_col is None:
            return None, None, [
                f"{strategy_id}.{table_name} 无可用时间戳列 "
                f"（候选: {_SIGNAL_TS_CANDIDATES}），effective_signals 记 0"
            ]

        return table_name, ts_col, []

    async def _query_signal_count(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple:
        """查询策略信号表中的有效信号数

        探测流程见 _probe_signal_table_and_col；计数走公共 _count_rows_in_window。
        信号按 schema 隔离，不需要 strategy_id 过滤。
        """
        table_name, ts_col, gap = await self._probe_signal_table_and_col(strategy_id)
        if table_name is None:
            return 0, gap

        try:
            # 按 strategy_id 查表决定是否追加状态过滤：
            #   - 已声明 → AND status IN (s1, s2, ...)
            #   - 未声明（如 hrs，其 status 域为 triggered/executed/timeout/conflict 独立口径）
            #     → 走纯 COUNT(*)，保持原有计数口径不变
            statuses = self.effective_signal_statuses.get(strategy_id)
            extra_condition = ""
            if statuses:
                quoted = ",".join(f"'{s}'" for s in statuses)
                extra_condition = f" AND status IN ({quoted})"

            cnt = await self._count_rows_in_window(
                strategy_id, table_name, ts_col, window_start, window_end,
                extra_condition=extra_condition,
            )
            return cnt, []
        except Exception as exc:
            logger.warning("信号计数查询失败", schema=strategy_id, table=table_name,
                           ts_col=ts_col, error=str(exc))
            return 0, [f"{strategy_id}.{table_name} 信号计数查询失败: {exc}"]

    # ---------------------------------------------------------------
    # K 线覆盖度（委托 kline_coverage.compute_kline_coverage）
    # ---------------------------------------------------------------

    async def _query_kline_coverage(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple:
        """按策略 universe 逐 symbol 加权计算 K 线覆盖

        委托 kline_coverage.compute_kline_coverage
        （内部走 UniverseResolver + generate_series + COUNT DISTINCT open_time）。

        Returns:
            (weighted_coverage, gaps_list, symbols_detail_list)
        """
        from . import kline_coverage as kc

        try:
            result = await kc.compute_kline_coverage(
                self.db,
                strategy_id=strategy_id,
                universe_cfg=self.universe_cfg or None,
                project_root=self.project_root,
                window_start=window_start,
                window_end=window_end,
            )
        except Exception as exc:
            logger.warning("K 线覆盖计算异常", strategy=strategy_id, error=str(exc))
            return 0.0, [f"K 线覆盖计算异常: {exc}"], []

        gaps = list(result.gaps)
        symbols_detail = [kc.symbol_row_to_dict(s) for s in result.symbols]
        return result.weighted_coverage, gaps, symbols_detail

    # ---------------------------------------------------------------
    # 门禁参数解析 / 主入口 / 单策略审计
    # ---------------------------------------------------------------

    def _resolve_snapshot_policy(self, strategy_id: str) -> tuple:
        """解析策略级外部快照门禁参数（per_strategy 覆盖优先于全局默认）

        Returns:
            (threshold, requirement)：生效阈值（float）与 requirement 代码（str）
        """
        entry = self.per_strategy.get(strategy_id, {})
        threshold = float(
            entry.get("min_external_snapshot_coverage",
                      self.readiness_cfg["min_external_snapshot_coverage"])
        )
        requirement = str(
            entry.get("external_snapshot_requirement",
                      self.readiness_cfg["external_snapshot_requirement"])
        )
        return threshold, requirement

    async def run(self, as_of: Optional[datetime] = None) -> ReadinessResult:
        """执行全部候选策略的准备度审计（入口）

        window_days 从 config 强制键 readiness.window_days 读取，
        已加入 _REQUIRED_READINESS_KEYS，缺失即 ValueError。
        """
        if as_of is None:
            as_of = datetime.now(timezone.utc)
        window_days = self.readiness_cfg["window_days"]
        window_start = as_of - timedelta(days=window_days)
        window_end = as_of

        candidates_result: Dict[str, CandidateReadiness] = {}
        for sid in self.candidates:
            cr = await self._audit_single(sid, window_start, window_end)
            candidates_result[sid] = cr

        selected = self._select_strategy(candidates_result)
        decision = "SELECT" if selected else "STOP"

        return ReadinessResult(
            as_of=as_of.isoformat(),
            window_days=window_days,
            decision=decision,
            selected_strategy=selected,
            candidates=candidates_result,
        )

    async def _audit_single(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> CandidateReadiness:
        """对单个候选策略执行全部门禁检查（调度子步骤）"""
        closed_trades, complete_ratio = await self._query_trade_records(
            strategy_id, window_start, window_end
        )
        signals_count, signal_gaps = await self._query_signal_count(
            strategy_id, window_start, window_end
        )
        # M6 标注：regime_count 当前是 proxy，TODO(阶段二)
        regime_proxy, regime_gaps = await self._query_regime_coverage(
            strategy_id, window_start, window_end
        )
        kline_coverage, kline_gaps, kline_symbols = await self._query_kline_coverage(
            strategy_id, window_start, window_end
        )
        snapshot_result = await self._query_external_snapshot(
            strategy_id, window_start, window_end
        )
        return self._build_candidate(
            strategy_id=strategy_id,
            closed_trades=closed_trades, complete_ratio=complete_ratio,
            signals_count=signals_count, signal_gaps=signal_gaps,
            regime_proxy=regime_proxy, regime_gaps=regime_gaps,
            kline_coverage=kline_coverage, kline_gaps=kline_gaps,
            kline_symbols=kline_symbols,
            snapshot_coverage=snapshot_result.coverage,
            snapshot_gaps=snapshot_result.gaps,
            snapshot_result=snapshot_result,
        )

    def _build_candidate(
        self,
        *,
        strategy_id: str,
        closed_trades: int, complete_ratio: float,
        signals_count: int, signal_gaps: List[str],
        regime_proxy: int, regime_gaps: List[str],
        kline_coverage: float, kline_gaps: List[str],
        kline_symbols: list,
        snapshot_coverage: float, snapshot_gaps: List[str],
        snapshot_result: Optional[SnapshotCoverage] = None,
    ) -> CandidateReadiness:
        """把各查询结果组装成 CandidateReadiness（门禁判定 + gaps 汇总 + 返回）"""
        identity_ratio = self.identity_baseline
        snap_threshold, snap_requirement = self._resolve_snapshot_policy(strategy_id)

        failures, snap_reason = self._apply_gate_thresholds(
            closed_trades, signals_count, complete_ratio,
            identity_ratio, kline_coverage, snapshot_coverage,
            regime_proxy, snap_threshold, snap_requirement,
        )
        gaps = self._build_gaps_list(
            signal_gaps, kline_gaps, snapshot_gaps, regime_gaps,
            identity_ratio, snapshot_coverage, snap_requirement,
        )
        return CandidateReadiness(
            strategy_id=strategy_id,
            closed_trades=closed_trades,
            effective_signals=signals_count,
            regime_count=regime_proxy,
            complete_trade_ratio=round(complete_ratio, 4),
            config_identity_ratio=round(identity_ratio, 4),
            kline_coverage=round(kline_coverage, 4),
            external_snapshot_coverage=round(snapshot_coverage, 4),
            external_snapshot_requirement=snap_requirement,
            external_snapshot_reason=snap_reason,
            external_snapshot_expected=(snapshot_result.expected if snapshot_result else 0),
            external_snapshot_complete=(snapshot_result.complete if snapshot_result else 0),
            external_snapshot_evidence_start=(
                snapshot_result.evidence_start.isoformat()
                if snapshot_result and snapshot_result.evidence_start else None
            ),
            external_snapshot_sources=(snapshot_result.sources if snapshot_result else []),
            failures=failures,
            gaps=gaps,
            kline_symbols=list(kline_symbols or []),
        )

    def _apply_gate_thresholds(
        self,
        closed_trades: int, signals_count: int, complete_ratio: float,
        identity_ratio: float, kline_coverage: float, snapshot_coverage: float,
        regime_count: int, snapshot_threshold: float, snapshot_requirement: str,
    ) -> tuple:
        """门禁判定：把各项指标与 config thresholds 比较（所有阈值从 config 读取）"""
        cfg = self.readiness_cfg
        failures: List[str] = []
        snap_reason = ""
        if closed_trades < cfg["min_closed_trades"]:
            failures.append(f"已平仓交易 {closed_trades} < {cfg['min_closed_trades']}")
        if signals_count < cfg["min_effective_signals"]:
            failures.append(f"有效信号 {signals_count} < {cfg['min_effective_signals']}")
        if complete_ratio < cfg["min_trade_field_completeness"]:
            failures.append(
                f"交易字段完整度 {format_ratio(complete_ratio)} "
                f"< {format_ratio(cfg['min_trade_field_completeness'])}"
            )
        if identity_ratio < cfg["min_config_identity_coverage"]:
            failures.append(
                f"配置身份覆盖 {format_ratio(identity_ratio)} "
                f"< {format_ratio(cfg['min_config_identity_coverage'])}"
            )
        if kline_coverage < cfg["min_kline_coverage"]:
            failures.append(
                f"K线覆盖 {format_ratio(kline_coverage)} "
                f"< {format_ratio(cfg['min_kline_coverage'])}"
            )
        if snapshot_requirement == "not_required_for_current_manifest":
            snap_reason = "not_required_for_current_manifest"
        elif snapshot_coverage < snapshot_threshold:
            failures.append(
                f"外部快照覆盖 {format_ratio(snapshot_coverage)} "
                f"< {format_ratio(snapshot_threshold)} "
                f"(required_historical_external_snapshots_missing)"
            )
            snap_reason = "required_historical_external_snapshots_missing"
        else:
            snap_reason = "snapshot_coverage_ok"
        if regime_count < cfg["min_regime_count"]:
            failures.append(f"regime_count(proxy) {regime_count} < {cfg['min_regime_count']}")
        return failures, snap_reason

    @staticmethod
    def _build_gaps_list(
        signal_gaps: List[str],
        kline_gaps: List[str],
        snapshot_gaps: List[str],
        regime_gaps: List[str],
        identity_ratio: float,
        snapshot_coverage: float,
        snapshot_requirement: str,
    ) -> List[str]:
        """组装 gaps 列表（含 proxy 标注、identity inferred 标注和快照门禁说明）"""
        gaps = list(signal_gaps) + list(kline_gaps)
        if snapshot_requirement == "not_required_for_current_manifest":
            gaps.append(
                f"外部快照 coverage={format_ratio(snapshot_coverage)} "
                f"（not_required_for_current_manifest：当前 manifest 的入场逻辑"
                f"不依赖 OI/市值/资金费率/情绪快照）"
            )
        else:
            gaps.extend(snapshot_gaps)
        gaps += list(regime_gaps)
        if identity_ratio < 1.0:
            gaps.append(
                f"配置身份用 inferred（无版本列），当前 baseline={identity_ratio:.2f}，"
                f"实际覆盖度可能被高估；TODO(阶段二): 补齐 identity 列后改为动态计算"
            )
        gaps.append(
            "regime_count 是 proxy（COUNT DISTINCT symbol），非真实市场状态覆盖数；"
            "TODO(阶段二): 引入 market_regime 表后改为真实计数"
        )
        return gaps

    # ---------------------------------------------------------------
    # 其余查询方法（trade_records / regime / 外部快照）
    # ---------------------------------------------------------------

    async def _fetch_trading_window(
        self, sql: str, strategy_id: str,
        window_start: datetime, window_end: datetime,
    ) -> Optional[dict]:
        """按 executed_at 列类型绑定窗口后查询 trading.trade_records"""
        ws, we = await db_probe.bind_bounds(
            self.db, "trading", "trade_records", "executed_at",
            window_start, window_end,
        )
        return await self.db.fetch_one(sql, strategy_id, ws, we)

    async def _query_trade_records(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple:
        """查询 trade_records 中的已平仓交易数和必填字段完整度"""
        sql = """
        SELECT COUNT(*) AS closed_trades,
               AVG(CASE WHEN symbol IS NOT NULL AND side IS NOT NULL
                             AND quantity > 0 AND price > 0
                             AND realized_pnl IS NOT NULL AND executed_at IS NOT NULL
                        THEN 1.0 ELSE 0.0 END) AS complete_ratio
        FROM trading.trade_records
        WHERE strategy = $1
          AND executed_at >= $2
          AND executed_at <  $3
          AND side IS NOT NULL
        """
        try:
            row = await self._fetch_trading_window(sql, strategy_id, window_start, window_end)
            if row is None:
                return 0, 1.0
            return int(row["closed_trades"] or 0), float(row["complete_ratio"] or 1.0)
        except Exception as exc:
            logger.warning("trade_records 查询失败", strategy_id=strategy_id,
                           error=str(exc))
            return 0, 0.0

    async def _query_regime_coverage(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple:
        """查询窗口内覆盖的"市场状态数"（proxy：COUNT DISTINCT symbol）"""
        sql = """
        SELECT COUNT(DISTINCT symbol) AS syms
        FROM trading.trade_records
        WHERE strategy = $1
          AND executed_at >= $2
          AND executed_at <  $3
        """
        try:
            row = await self._fetch_trading_window(sql, strategy_id, window_start, window_end)
            return int((row or {}).get("syms") or 0), []
        except Exception as exc:
            logger.warning("regime_count 查询失败", strategy=strategy_id,
                           error=str(exc))
            return 0, [f"regime_count(proxy) 查询失败，覆盖度记 0：{exc}"]

    async def _query_external_snapshot(
        self, strategy_id: str, window_start: datetime, window_end: datetime
    ) -> SnapshotCoverage:
        """按历史 universe 的 symbol-hour 计算完整外部快照覆盖率。"""
        sql = """
        WITH scan_runs AS (
            SELECT observed_at,
                   LEAD(observed_at, 1, $3) OVER (ORDER BY observed_at) AS valid_until
            FROM public.ai_tuner_universe_scan_runs
            WHERE strategy_id = $1 AND observed_at < $3 AND created_at <= $3
        ), revisions AS (
            SELECT u.strategy_id, u.symbol, u.observed_at, r.valid_until
            FROM public.ai_tuner_universe_snapshots
            AS u
            JOIN scan_runs r ON r.observed_at = u.observed_at
            WHERE u.strategy_id = $1 AND u.observed_at < $3 AND u.created_at <= $3
        ), universe_hours AS (
            SELECT DISTINCT r.symbol, hour.bucket_start
            FROM revisions r
            CROSS JOIN LATERAL generate_series(
                GREATEST(date_trunc('hour', r.observed_at), date_trunc('hour', $2)),
                LEAST(
                    date_trunc('hour', r.valid_until - interval '1 microsecond'),
                    date_trunc('hour', $3 - interval '1 microsecond')
                ),
                interval '1 hour'
            ) AS hour(bucket_start)
            WHERE r.valid_until > $2
        ), evidence AS (
            SELECT u.symbol, u.bucket_start,
                   m.oi_source, m.funding_source, m.market_cap_source,
                   CASE WHEN m.open_interest_usd > 0
                              AND m.funding_rate IS NOT NULL
                              AND m.market_cap_usd > 0
                              AND m.oi_market_cap_ratio IS NOT NULL
                              AND m.sentiment_score IS NOT NULL
                              AND m.sentiment_formula_version = 'funding_annual_pct_v1'
                              AND ABS(
                                  m.oi_market_cap_ratio
                                  - (m.open_interest_usd / NULLIF(m.market_cap_usd, 0))
                              ) <= 1e-9 * GREATEST(
                                  1.0, ABS(m.open_interest_usd / NULLIF(m.market_cap_usd, 0))
                              )
                              AND ABS(
                                  m.sentiment_score
                                  - (m.funding_rate * 3 * 365 * 100)
                              ) <= 1e-9 * GREATEST(
                                  1.0, ABS(m.funding_rate * 3 * 365 * 100)
                              )
                              AND m.market_cap_source IN ('coingecko', 'cache_hit', 'coingecko_cache')
                              AND m.source_observed_at <= u.bucket_start + interval '1 hour'
                              AND m.source_observed_at <= $3
                        THEN 1 ELSE 0 END AS complete
            FROM universe_hours u
            LEFT JOIN public.ai_tuner_market_snapshots m
              ON m.strategy_id = $1 AND m.symbol = u.symbol
             AND m.bucket_start = u.bucket_start
             AND m.collected_at <= $3
        )
        SELECT COUNT(*) AS symbol_hours_expected,
               COALESCE(SUM(complete), 0) AS complete,
               MIN(bucket_start) AS evidence_start,
               EXISTS(
                   SELECT 1 FROM scan_runs WHERE observed_at <= $2
               ) AS has_window_start_universe,
               (SELECT MIN(observed_at) FROM scan_runs) AS first_scan_at,
               ARRAY_REMOVE(ARRAY_AGG(DISTINCT oi_source), NULL) ||
               ARRAY_REMOVE(ARRAY_AGG(DISTINCT funding_source), NULL) ||
               ARRAY_REMOVE(ARRAY_AGG(DISTINCT market_cap_source), NULL) AS sources
        FROM evidence
        """
        try:
            row = await self.db.fetch_one(sql, strategy_id, window_start, window_end)
            expected = int(
                (row or {}).get("symbol_hours_expected")
                or (row or {}).get("expected")
                or 0
            )
            complete = int((row or {}).get("complete") or 0)
            has_window_start_universe = bool(
                (row or {}).get("has_window_start_universe", False)
            )
            first_scan_at = (row or {}).get("first_scan_at")
            sources = sorted(set((row or {}).get("sources") or []))
            if expected == 0 or not has_window_start_universe:
                missing_marker = (
                    f"historical_universe_missing_before={first_scan_at.isoformat()}"
                    if first_scan_at else "historical_universe_missing"
                )
                return SnapshotCoverage(
                    expected, complete, 0.0,
                    (row or {}).get("evidence_start"), sources, [
                        missing_marker,
                        "无历史外部快照表或窗口内无历史 HRS universe 快照",
                    ]
                )
            return SnapshotCoverage(
                expected, complete, min(complete / expected, 1.0),
                (row or {}).get("evidence_start"), sources, [],
            )
        except Exception as exc:
            logger.warning("外部快照覆盖查询失败", strategy=strategy_id, error=str(exc))
            return SnapshotCoverage(
                0, 0, 0.0, None, [],
                [f"external_snapshot_query_failed：{exc}"],
            )

    # ---------------------------------------------------------------
    # 策略选择（确定性顺序）
    # ---------------------------------------------------------------

    @staticmethod
    def _select_strategy(candidates: Dict[str, CandidateReadiness]) -> Optional[str]:
        """按 config 顺序选第一个 eligible 的策略；都不合格返回 None（STOP）"""
        for sid in candidates.keys():
            cr = candidates.get(sid)
            if cr and cr.eligible:
                return sid
        return None
