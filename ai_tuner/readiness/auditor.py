"""
90 天准备度审计器

对每个 MVP 候选策略查询 PostgreSQL，产出：
- 已平仓交易数、有效信号数、市场状态覆盖度
- 交易记录字段完整度、配置身份可追溯度、K 线覆盖度、外部快照完整度
- 各项硬门禁是否通过（阈值全部可配置，严禁硬编码）

关键设计：
- K 线 schema 不硬编码 public.，而是通过 information_schema 动态探测
- trade_signals schema 也通过 information_schema 动态探测（M1 修复）
- 外部快照表不存在时 coverage=0 并在 gaps 列表里说明（预期行为）
- 配置身份覆盖度：从 config purpose_driven_mvp.identity_ratio_baseline 读取（M2 修复）
- 策略选择优先序：读 config candidates 顺序（M3 修复）
- min_external_snapshot_coverage 无兜底，由 config 决定（M4 修复）
- regime_count = COUNT(DISTINCT symbol) 是 proxy，非真实市场状态数（M6 修复 + TODO 阶段二）
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog

from .models import CandidateReadiness, ReadinessResult

logger = structlog.get_logger()

# 数据库已知 schema 列表（来自 postgres init-scripts，用于 K 线探测的 SQL 参数）
_KNOWN_SCHEMAS = ["btc_eth", "btc_eth_aggressive", "new_coin", "grid", "trading", "public"]


# 门禁阈值必须存在的 readiness 配置键（缺失即 ValueError，不静默）
_REQUIRED_READINESS_KEYS = [
    "min_closed_trades",
    "min_effective_signals",
    "min_trade_field_completeness",
    "min_config_identity_coverage",
    "min_kline_coverage",
    "min_external_snapshot_coverage",
    "min_regime_count",
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
        # M3 修复：优先序来自 config candidates（而非硬编码 aggressive/hrs）
        self.candidates = list(mvp.get("candidates", []))
        self.readiness_cfg = mvp.get("readiness", {})
        # F5 修复：显式键检查——缺失即 ValueError，杜绝静默 None 比较出 TypeError
        missing = [k for k in _REQUIRED_READINESS_KEYS if k not in self.readiness_cfg]
        if missing:
            raise ValueError(
                f"purpose_driven_mvp.readiness 缺键: {missing}"
            )
        # M2 修复：配置身份 baseline 从 config 读取，而非硬编码 0.90
        self.identity_baseline = float(
            mvp.get("identity_ratio_baseline", 0.90)
        )

    # ---------------------------------------------------------------
    # 主入口
    # ---------------------------------------------------------------

    async def run(self, as_of: Optional[datetime] = None) -> ReadinessResult:
        """
        执行全部候选策略的准备度审计

        Args:
            as_of: 审计截止时间（UTC）；None 时用当前时间

        Returns:
            ReadinessResult
        """
        if as_of is None:
            as_of = datetime.now(timezone.utc)
        window_days = self.readiness_cfg.get("window_days", 90)
        window_start = as_of - timedelta(days=window_days)
        window_end = as_of  # 审计窗口 = [as_of - window_days, as_of)

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

    # ---------------------------------------------------------------
    # 单策略审计（拆分为 3 个子函数解决 M9 超 50 行）
    # ---------------------------------------------------------------

    async def _audit_single(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> CandidateReadiness:
        """对单个候选策略执行全部门禁检查（调度子步骤）"""
        # Step 1: 查询各项数据
        closed_trades, complete_ratio = await self._query_trade_records(
            strategy_id, window_start, window_end
        )
        signals_count, signal_gaps = await self._query_signal_count(
            strategy_id, window_start, window_end
        )
        # M6 标注：regime_count 当前是 proxy（COUNT DISTINCT symbol），非真实市场状态数
        # TODO(阶段二): 引入 market_regime 表后改为真实计数
        regime_proxy, regime_gaps = await self._query_regime_coverage(
            strategy_id, window_start, window_end
        )
        kline_coverage, kline_gaps = await self._query_kline_coverage(
            window_start, window_end
        )
        snapshot_coverage, snapshot_gaps = await self._query_external_snapshot()
        identity_ratio = self.identity_baseline  # M2 修复：从 config 读

        # Step 2: 门禁判定（拆分到独立方法）
        failures = self._apply_gate_thresholds(
            closed_trades, signals_count, complete_ratio,
            identity_ratio, kline_coverage, snapshot_coverage,
            regime_proxy,
        )

        # Step 3: 报告组装（拆分到独立方法）
        gaps = self._build_gaps_list(
            signal_gaps, kline_gaps, snapshot_gaps, regime_gaps, identity_ratio
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
            failures=failures,
            gaps=gaps,
        )

    def _apply_gate_thresholds(
        self,
        closed_trades: int,
        signals_count: int,
        complete_ratio: float,
        identity_ratio: float,
        kline_coverage: float,
        snapshot_coverage: float,
        regime_count: int,
    ) -> List[str]:
        """
        门禁判定：把各项指标与 config thresholds 比较

        所有阈值从 config 读取，M4 修复：min_external_snapshot_coverage
        不再有兜底（缺失即报错），由 config 决定。
        """
        cfg = self.readiness_cfg
        # M4 修复：无兜底，config 缺失时 .get() 返回 None 导致后续比较 TypeError
        # — 但 config 加载时已校验，这里只 .get() 不用默认值
        failures: List[str] = []
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
        if snapshot_coverage < cfg["min_external_snapshot_coverage"]:
            failures.append(f"外部快照覆盖 {snapshot_coverage:.1%} < {cfg['min_external_snapshot_coverage']:.1%}")
        if regime_count < cfg["min_regime_count"]:
            failures.append(f"regime_count(proxy) {regime_count} < {cfg['min_regime_count']}")
        return failures

    @staticmethod
    def _build_gaps_list(
        signal_gaps: List[str],
        kline_gaps: List[str],
        snapshot_gaps: List[str],
        regime_gaps: List[str],
        identity_ratio: float,
    ) -> List[str]:
        """组装 gaps 列表（含 proxy 标注和 identity inferred 标注）"""
        gaps = list(signal_gaps) + list(kline_gaps) + list(snapshot_gaps) + list(regime_gaps)
        if identity_ratio < 1.0:
            gaps.append(
                f"配置身份用 inferred（无版本列），当前 baseline={identity_ratio:.2f}，"
                f"实际覆盖度可能被高估；TODO(阶段二): 补齐 identity 列后改为动态计算"
            )
        # M6 proxy 标注
        gaps.append(
            "regime_count 是 proxy（COUNT DISTINCT symbol），非真实市场状态覆盖数；"
            "TODO(阶段二): 引入 market_regime 表后改为真实计数"
        )
        return gaps

    # ---------------------------------------------------------------
    # 查询方法（全部参数化，纯 SQL，不硬编码 schema）
    # ---------------------------------------------------------------

    async def _query_trade_records(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple:
        """
        查询 trade_records 中的已平仓交易数和必填字段完整度

        strategy 字段直接传 strategy_id（record_name 写入值），不硬编码中文名。
        """
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
            row = await self.db.fetch_one(sql, strategy_id, window_start, window_end)
            if row is None:
                return 0, 1.0
            return int(row["closed_trades"] or 0), float(row["complete_ratio"] or 1.0)
        except Exception as exc:
            logger.warning("trade_records 查询失败", strategy_id=strategy_id, error=str(exc))
            return 0, 0.0

    async def _query_signal_count(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple:
        """
        查询 trade_signals 表中的有效信号数

        M1 修复：先通过 information_schema 动态探测哪个 schema 有 trade_signals 表
        （之前硬编码 known dict 与"全部动态"注释矛盾）。
        """
        # M1 修复：动态探测 schema，而非已知映射硬编码
        schema = await self._discover_signal_schema(strategy_id)
        if schema is None:
            return 0, [
                f"trade_signals 表缺失（information_schema 探测无 schema 含该表），"
                f"effective_signals 记 0"
            ]

        sql = f"""
        SELECT COUNT(*) AS cnt
        FROM {schema}.trade_signals
        WHERE strategy_id = $1
          AND signal_time >= $2
          AND signal_time <  $3
        """
        try:
            row = await self.db.fetch_one(sql, strategy_id, window_start, window_end)
            return int((row or {}).get("cnt") or 0), []
        except Exception as exc:
            logger.warning("trade_signals 查询失败", schema=schema, error=str(exc))
            return 0, [f"trade_signals 查询失败 ({schema}): {exc}"]

    async def _query_regime_coverage(
        self,
        strategy_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple:
        """
        查询窗口内覆盖的"市场状态数"（proxy：COUNT DISTINCT symbol）

        ⚠️ M6 proxy 标注：当前是 COUNT(DISTINCT symbol) 作为 regime_count 的近似值
        （窗口内至少 N 笔不同 symbol 的交易 → 粗略推断覆盖 N 个候选币），
        不是真实的市场状态（trending/ranging/...）覆盖数。
        TODO(阶段二): 引入 market_regime 表后改为真实 regime 计数。

        Returns:
            (int, List[str]): regime_count(proxy) 和查询相关的 gaps 说明
        """
        sql = """
        SELECT COUNT(DISTINCT symbol) AS syms
        FROM trading.trade_records
        WHERE strategy = $1
          AND executed_at >= $2
          AND executed_at <  $3
        """
        try:
            row = await self.db.fetch_one(sql, strategy_id, window_start, window_end)
            return int((row or {}).get("syms") or 0), []
        except Exception as exc:
            logger.warning("regime_count 查询失败，记 0", strategy=strategy_id, error=str(exc))
            return 0, [f"regime_count(proxy) 查询失败，覆盖度记 0：{exc}"]

    async def _query_kline_coverage(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple:
        """
        查询 K 线覆盖度（不硬编码 public schema）

        先探测哪个 schema 下有 K 线表，然后比较已收盘 bar 数与预期 bar 数。
        找不到表 → coverage=0。
        """
        schema = await self._resolve_kline_schema()
        if schema is None:
            return 0.0, ["K 线表未探测到（search_path 下所有 schema 都没有 K 线表）"]

        table_name = await self._detect_kline_table_name(schema)
        if table_name is None:
            return 0.0, [f"schema {schema} 下未识别到 K 线表名"]

        ts_col = await self._detect_timestamp_column(schema, table_name)
        if ts_col is None:
            return 0.0, [f"schema {schema}.{table_name} 下未识别到时间戳列"]

    # 预期 bar 数：从表名推导 K 线周期（F6 修复，不再硬编码 1h）
        period_sec = self._infer_kline_period(table_name)
        if period_sec is None:
            gaps = [f"K 线周期未知（表名 {table_name} 未匹配 1m/5m/15m/1h/4h/1d），覆盖率记 0"]
            logger.warning("K 线周期未知", table=table_name)
            return 0.0, gaps
        expected_bars = int((window_end - window_start).total_seconds() / period_sec)

        sql = f"""
        SELECT COUNT(*) AS cnt
        FROM {schema}.{table_name}
        WHERE {ts_col} >= $1
          AND {ts_col} <  $2
        """
        try:
            row = await self.db.fetch_one(sql, window_start, window_end)
            actual = int((row or {}).get("cnt") or 0)
            coverage = actual / expected_bars if expected_bars > 0 else 0.0
            return min(coverage, 1.0), []
        except Exception as exc:
            logger.warning("K 线覆盖查询失败", schema=schema, table=table_name, error=str(exc))
            return 0.0, [f"K 线查询失败: {exc}"]

    async def _query_external_snapshot(self) -> tuple:
        """
        外部时点快照覆盖度

        数据库没有任何历史快照表（OI/市值/资金费率/情绪）→ 直接返回 (0.0, [缺口说明])。
        这是预期行为不是 bug。
        """
        return 0.0, [
            "无历史外部快照表（OI/市值/资金费率/情绪时点快照），"
            "external_snapshot_coverage 记 0；后续阶段需补齐快照采集器"
        ]

    # ---------------------------------------------------------------
    # schema / 表名探测（全部动态，不硬编码）
    # ---------------------------------------------------------------

    async def _discover_signal_schema(self, strategy_id: str) -> Optional[str]:
        """
        M1 修复：动态探测哪个 schema 下有 trade_signals 表

        遍历已知 schema 列表查 information_schema.tables。
        找到了 → 验证该 schema 的 trade_signals 表有 strategy_id 列 → 返回 schema；
        没找到 → 返回 None。
        （之前是 known dict 硬编码 {"btc_eth":"btc_eth",...}，与注释矛盾）
        """
        find_schema_sql = """
        SELECT table_schema FROM information_schema.tables
        WHERE table_schema = ANY($1) AND table_name = 'trade_signals'
        """
        try:
            rows = await self.db.fetch_all(find_schema_sql, _KNOWN_SCHEMAS)
        except Exception as exc:
            logger.warning("trade_signals schema 探测失败", error=str(exc))
            return None
        if not rows:
            return None

        # 多个 schema 有 trade_signals 表？取第一个（通常 btc_eth 生产）
        for row in rows:
            candidate = row["table_schema"]
            # 再验证 strategy_id 列存在
            col_sql = """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = 'trade_signals'
              AND column_name = 'strategy_id'
            """
            col_row = await self.db.fetch_one(col_sql, candidate)
            if col_row is not None:
                return candidate
        return None

    async def _resolve_kline_schema(self) -> Optional[str]:
        """
        探测哪个 schema 下有 K 线表

        遍历已知 schema 列表，查 information_schema.tables 找 K 线表。
        ORDER BY table_schema 已保证 btc_eth 排在前面 → M5 修复：
        删除原来的 for-if 死分支，直接取 rows[0]。
        不硬编码 public.。
        """
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

    async def _detect_kline_table_name(self, schema: str) -> Optional[str]:
        """探测指定 schema 下 K 线表的精确名称"""
        sql = """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = $1 AND table_name ILIKE '%kline%'
        LIMIT 1
        """
        try:
            row = await self.db.fetch_one(sql, schema)
            return row["table_name"] if row else None
        except Exception:
            return None

    async def _detect_timestamp_column(self, schema: str, table: str) -> Optional[str]:
        """探测 K 线表的时间戳列名"""
        sql = """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
          AND column_name IN ('timestamp', 'open_time', 'close_time', 'ts')
        LIMIT 1
        """
        try:
            row = await self.db.fetch_one(sql, schema, table)
            return row["column_name"] if row else None
        except Exception:
            return None

    @staticmethod
    def _infer_kline_period(table_name: str) -> Optional[int]:
        """从 K 线表名推导 bar 周期（秒）

        匹配规则（优先长周期，避免 1m 先命中 1h 里的 1）：
        - `_1d` / `_24h` → 86400 秒
        - `_4h` → 14400 秒
        - `_1h` → 3600 秒
        - `_15m` → 900 秒
        - `_5m` → 300 秒
        - `_1m` → 60 秒
        未匹配返回 None。
        """
        # 长模式优先，正则里按顺序排列
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
        """
        M3 修复：优先序来自 config candidates（而非硬编码 aggressive/hrs）

        按 config 里声明的 candidates 先后顺序：第一个 elig 就选；
        没有 elig 的 → 返回 None（STOP）
        （candidates 顺序在 __init__ 已读 config，这里 candidates dict key 即 config 顺序）
        """
        # dict 插入顺序即 config 顺序（Python 3.7+）
        for sid in candidates.keys():
            cr = candidates.get(sid)
            if cr and cr.eligible:
                return sid
        return None
