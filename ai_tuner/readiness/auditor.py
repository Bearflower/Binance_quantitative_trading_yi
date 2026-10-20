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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import structlog

from .models import CandidateReadiness, ReadinessResult

logger = structlog.get_logger()

# ---------- 模块常量 ----------

# 数据库已知 schema 列表（来自 postgres init-scripts，用于 K 线探测的 SQL 参数）
_KNOWN_SCHEMAS = ["btc_eth", "btc_eth_aggressive", "new_coin", "grid", "trading", "public"]

# 信号表候选时间列名（按固定优先级匹配）
_SIGNAL_TS_CANDIDATES = ["created_at", "signal_time", "triggered_at", "executed_at"]

# 信号表候选表名后缀（按优先级匹配，覆盖 trade_signals / hrs_signals / short_signals）
_SIGNAL_TABLE_PATTERN = "%signal%"

# information_schema.columns.data_type aware 字面量（模块常量，避免散写）
# naive 分支用 None/降级处理，不单独声明常量
_TS_AWARE = "timestamp with time zone"

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


class ReadinessAuditor:
    """执行 90 天准备度审计"""

    def __init__(self, db_manager, config: Dict[str, Any]):
        """
        Args:
            db_manager: DatabaseManager 实例（异步）
            config: 完整 ai_tuner/config.yaml 内容
        """
        self.db = db_manager
        mvp = config.get("purpose_driven_mvp", {})

        # mvp 顶层强制键校验（含 identity_ratio_baseline）
        missing_mvp = [k for k in _REQUIRED_MVP_KEYS if k not in mvp]
        if missing_mvp:
            raise ValueError(
                f"purpose_driven_mvp 缺键: {missing_mvp}"
            )

        self.candidates = list(mvp["candidates"])
        self.identity_baseline = float(mvp["identity_ratio_baseline"])
        self.readiness_cfg = mvp.get("readiness", {})

        # readiness 段强制键校验（含 window_days，不再 get 兜底）
        missing = [k for k in _REQUIRED_READINESS_KEYS if k not in self.readiness_cfg]
        if missing:
            raise ValueError(
                f"purpose_driven_mvp.readiness 缺键: {missing}"
            )

        # 策略级外部快照覆盖（per_strategy）：可选，但每个条目必须含必备键
        self.per_strategy: Dict[str, dict] = {}
        per_cfg = self.readiness_cfg.get("per_strategy", {}) or {}
        for sid, entry in per_cfg.items():
            missing_keys = [k for k in _REQUIRED_PER_STRATEGY_KEYS if k not in entry]
            if missing_keys:
                raise ValueError(
                    f"purpose_driven_mvp.readiness.per_strategy.{sid} 缺键: {missing_keys}"
                )
            self.per_strategy[sid] = dict(entry)

        # 策略级「有效信号」状态过滤集合（可选，不在 _REQUIRED_READINESS_KEYS）。
        # 仅声明了的策略（dict 中含 sid）会在信号计数时追加 AND status IN (...)；
        # 未声明者（如 hrs，其状态域 triggered/executed/timeout/conflict 与 MTPCS 完全不同）
        # 保持纯 COUNT(*) 口径不变——禁止硬编码，策略状态集合差异由配置声明。
        self.effective_signal_statuses = _validate_effective_signal_statuses(
            self.readiness_cfg.get("effective_signal_statuses")
        )

    # ---------------------------------------------------------------
    # 辅助：schema / 表 / 列类型探测 + 时间绑定 + 公共窗口计数
    # ---------------------------------------------------------------

    async def _fetch_column_data_type(
        self, schema: str, table: str, column: str
    ) -> Optional[str]:
        """通过 information_schema.columns 探测目标列的 data_type"""
        sql = """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2 AND column_name = $3
        """
        try:
            row = await self.db.fetch_one(sql, schema, table, column)
            return row["data_type"] if row else None
        except Exception as exc:
            logger.warning("列类型探测失败", schema=schema, table=table,
                           column=column, error=str(exc))
            return None

    async def _bind_window(
        self, schema: str, table: str, column: str, dt: datetime
    ) -> datetime:
        """按目标时间列类型转换窗口绑定值（消除 asyncpg naive/aware 冲突）

        naive 列 → dt.replace(tzinfo=None)；
        aware 列 → dt.astimezone(timezone.utc)（dt 无 tz 则补 UTC）；
        探测失败 → 降级 naive 去 tzinfo（保证不炸）。
        公共方法，多处复用：trade_records / 信号计数 / K 线计数。
        """
        dtype = await self._fetch_column_data_type(schema, table, column)
        if dtype == _TS_AWARE:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        # 显式 naive 列（_TS_NAIVE）或探测失败（None）→ 去 tzinfo，
        # 统一处理保证 asyncpg 不炸；其他 data_type（如 "timestamp" 无 precision）
        # 也降级走这条，做防御性兜底。aware/naive 分支语义对称，便于维护
        return dt.replace(tzinfo=None)

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

    async def _detect_dominant_symbol(
        self, strategy_id: str, window_start: datetime, window_end: datetime
    ) -> Optional[str]:
        """查询窗口内该策略的主力合约（symbol 出现次数最多者）

        用于 K 线表定位：避免任意取第一张 K 线表（经常是无关小币）。
        """
        sql = """
        SELECT symbol
        FROM trading.trade_records
        WHERE strategy = $1 AND executed_at >= $2 AND executed_at < $3
        GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT 1
        """
        try:
            ws = await self._bind_window("trading", "trade_records",
                                         "executed_at", window_start)
            we = await self._bind_window("trading", "trade_records",
                                         "executed_at", window_end)
            row = await self.db.fetch_one(sql, strategy_id, ws, we)
            return row["symbol"] if row else None
        except Exception as exc:
            logger.warning("主力合约查询失败", strategy=strategy_id,
                           error=str(exc))
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
    # K 线表定位 + 覆盖度（拆成 locate / coverage 两步，各 ≤50 行）
    # ---------------------------------------------------------------

    async def _locate_kline_table(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> Tuple[Optional[str], Optional[str], List[str]]:
        """按主力合约定位 K 线表（独立方法，让 _query_kline_coverage 主体 ≤50 行）

        Returns:
            (schema_or_None, table_name_or_None, gaps_for_missing_cases)
            成功 → (schema, table_name, [])；失败 → (None, None, gaps)
        """
        # Step 1: 主力合约探测
        dominant = await self._detect_dominant_symbol(
            strategy_id, window_start, window_end
        )
        if dominant is None:
            return None, None, [
                f"K 线覆盖度无法计算：strategy={strategy_id} 在窗口内无交易记录"
                f"（主力合约探测为空）；TODO(阶段二): 改为按策略 universe 逐 symbol 计算"
            ]

        # Step 2: K 线 schema 探测（所有生产 K 线都在 btc_eth schema）
        schema = await self._resolve_kline_schema()
        if schema is None:
            return None, None, [
                "K 线表未探测到（search_path 下所有 schema 都没有 K 线表）"
            ]

        # Step 3: 按主力合约找 K 线表（优先 1h 周期，退而任意周期）
        table_name = await self._detect_kline_table_name(schema, dominant)
        if table_name is None:
            return None, None, [
                f"schema {schema} 下无匹配主力合约 {dominant} 的 K 线表"
                f"（pattern: kline_{dominant.lower()}_%）；"
                f"TODO(阶段二): 改为按策略 universe 逐 symbol 计算"
            ]

        return schema, table_name, []

    async def _query_kline_coverage(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple:
        """查询 K 线覆盖度（按主力合约取表）

        覆盖度含义：**主力合约抽样**，不是全候选池覆盖。
        TODO(阶段二): 改为按策略 universe 逐 symbol 计算覆盖率。
        """
        # Step 1-3: 定位 K 线表（主力合约 → schema → table）
        schema, table_name, gap = await self._locate_kline_table(
            strategy_id, window_start, window_end
        )
        if schema is None:
            return 0.0, gap

        # Step 4: 时间戳列探测
        ts_col = await self._detect_timestamp_column(schema, table_name)
        if ts_col is None:
            return 0.0, [f"schema {schema}.{table_name} 下未识别到时间戳列"]

        # Step 5: 周期推导 + 计数
        period_sec = self._infer_kline_period(table_name)
        if period_sec is None:
            logger.warning("K 线周期未知", table=table_name)
            return 0.0, [
                f"K 线周期未知（表名 {table_name} 未匹配 1m/5m/15m/1h/4h/1d），"
                f"覆盖率记 0"
            ]
        expected_bars = int((window_end - window_start).total_seconds() / period_sec)

        try:
            actual = await self._count_rows_in_window(
                schema, table_name, ts_col, window_start, window_end
            )
        except Exception as exc:
            logger.warning("K 线覆盖查询失败", schema=schema, table=table_name,
                           error=str(exc))
            return 0.0, [f"K 线查询失败: {exc}"]

        coverage = actual / expected_bars if expected_bars > 0 else 0.0
        return min(coverage, 1.0), []

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
        kline_coverage, kline_gaps = await self._query_kline_coverage(
            strategy_id, window_start, window_end
        )
        snapshot_coverage, snapshot_gaps = await self._query_external_snapshot()
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
            failures=failures,
            gaps=gaps,
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
            failures.append(f"交易字段完整度 {complete_ratio:.1%} < {cfg['min_trade_field_completeness']:.1%}")
        if identity_ratio < cfg["min_config_identity_coverage"]:
            failures.append(f"配置身份覆盖 {identity_ratio:.1%} < {cfg['min_config_identity_coverage']:.1%}")
        if kline_coverage < cfg["min_kline_coverage"]:
            failures.append(f"K线覆盖 {kline_coverage:.1%} < {cfg['min_kline_coverage']:.1%}")
        if snapshot_requirement == "not_required_for_current_manifest":
            snap_reason = "not_required_for_current_manifest"
        elif snapshot_coverage < snapshot_threshold:
            failures.append(
                f"外部快照覆盖 {snapshot_coverage:.1%} < {snapshot_threshold:.1%} "
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
                f"外部快照 coverage={snapshot_coverage:.1%} "
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
            ws = await self._bind_window("trading", "trade_records",
                                         "executed_at", window_start)
            we = await self._bind_window("trading", "trade_records",
                                         "executed_at", window_end)
            row = await self.db.fetch_one(sql, strategy_id, ws, we)
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
            ws = await self._bind_window("trading", "trade_records",
                                         "executed_at", window_start)
            we = await self._bind_window("trading", "trade_records",
                                         "executed_at", window_end)
            row = await self.db.fetch_one(sql, strategy_id, ws, we)
            return int((row or {}).get("syms") or 0), []
        except Exception as exc:
            logger.warning("regime_count 查询失败", strategy=strategy_id,
                           error=str(exc))
            return 0, [f"regime_count(proxy) 查询失败，覆盖度记 0：{exc}"]

    async def _query_external_snapshot(self) -> tuple:
        """外部时点快照覆盖度（无历史快照表是预期行为）"""
        return 0.0, [
            "无历史外部快照表（OI/市值/资金费率/情绪时点快照），"
            "external_snapshot_coverage 记 0；后续阶段需补齐快照采集器"
        ]

    # ---------------------------------------------------------------
    # K 线探测辅助（schema / table / timestamp / period）
    # ---------------------------------------------------------------

    async def _resolve_kline_schema(self) -> Optional[str]:
        """探测哪个 schema 下有 K 线表（ORDER BY table_schema → btc_eth 优先）"""
        sql = """
        SELECT table_schema
        FROM information_schema.tables
        WHERE table_schema = ANY($1)
          AND table_name ILIKE '%kline%'
        ORDER BY table_schema
        LIMIT 1
        """
        try:
            row = await self.db.fetch_one(sql, _KNOWN_SCHEMAS)
        except Exception as exc:
            logger.warning("K 线 schema 探测失败", error=str(exc))
            return None
        return row["table_schema"] if row else None

    async def _detect_kline_table_name(
        self, schema: str, dominant_symbol: str
    ) -> Optional[str]:
        """按主力合约 symbol 定位 K 线表（优先 1h 周期，退而任意周期）

        两段 SELECT 结构相同 → 用 pattern 列表循环消除重复代码（观察项 6）。
        """
        symbol_lower = dominant_symbol.lower()
        # 优先 1h（覆盖 90 天窗口 2160 bar），退而任意周期
        for pattern_like, label in [
            (f"kline_{symbol_lower}_%h", "1h"),
            (f"kline_{symbol_lower}_%", "任意"),
        ]:
            sql = """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = $1 AND table_name ILIKE $2
            ORDER BY table_name LIMIT 1
            """
            try:
                row = await self.db.fetch_one(sql, schema, pattern_like)
                if row:
                    return row["table_name"]
            except Exception as exc:
                logger.warning(
                    "按 symbol 探测 K 线表失败", schema=schema,
                    symbol=dominant_symbol, period=label, error=str(exc),
                )
        return None

    async def _detect_timestamp_column(self, schema: str, table: str) -> Optional[str]:
        """探测 K 线表的时间戳列名；异常时记录 warning 后返回 None"""
        sql = """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
          AND column_name IN ('timestamp', 'open_time', 'close_time', 'ts')
        LIMIT 1
        """
        try:
            row = await self.db.fetch_one(sql, schema, table)
            return row["column_name"] if row else None
        except Exception as exc:
            logger.warning("K 线时间戳列探测异常", schema=schema, table=table,
                           error=str(exc))
            return None

    @staticmethod
    def _infer_kline_period(table_name: str) -> Optional[int]:
        """从 K 线表名推导 bar 周期（秒）

        长模式优先：_1d(86400) → _24h(86400) → _4h(14400) → _1h(3600)
        → _15m(900) → _5m(300) → _1m(60)
        """
        period_map = [
            (re.compile(r"_1[dD]"), 86400),
            (re.compile(r"_24[hH]"), 86400),
            (re.compile(r"_4[hH]"), 14400),
            (re.compile(r"_1[hH]"), 3600),
            (re.compile(r"_15[mM]"), 900),
            (re.compile(r"_5[mM]"), 300),
            (re.compile(r"_1[mM]"), 60),
        ]
        for pattern, sec in period_map:
            if pattern.search(table_name):
                return sec
        return None

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
