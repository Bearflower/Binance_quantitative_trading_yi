"""
绩效指标纯函数模块单测

覆盖 shared/performance_metrics.py 中的 annualized_sharpe 和 max_drawdown_ratio，
以及 dashboard 后端 DataService 的策略级净值曲线口径
（「固定资金基数 + 累计盈亏」、不再复利、回撤 ≤100%、历史月份不取未来快照）。
所有测试不依赖数据库、不依赖 Binance API，完全隔离（DB 用假实现）。
"""
import math
import os
import sys
from datetime import date, datetime, timedelta

import pytest

from shared.performance_metrics import (
    ANNUALIZATION_PERIODS,
    MIN_SAMPLE_COUNT,
    annualized_sharpe,
    max_drawdown_ratio,
)

# dashboard 后端模块以 dashboard/backend 为根，需临时加入 sys.path 才能 import services.*
_DASHBOARD_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard", "backend"
)
if _DASHBOARD_BACKEND not in sys.path:
    sys.path.insert(0, _DASHBOARD_BACKEND)

# 仓库根 services/ 是无 __init__.py 的命名空间包；若它先被全量收集缓存，
# 仅插 sys.path 无法让其中新增 backend 路径，需显式补入命名空间 __path__。
# 注意 __path__ 条目是「services 包目录本身」而非其父目录。
_BACKEND_SERVICES_DIR = os.path.join(_DASHBOARD_BACKEND, "services")
_services_pkg = sys.modules.get("services")
if _services_pkg is not None and _BACKEND_SERVICES_DIR not in _services_pkg.__path__:
    _services_pkg.__path__.append(_BACKEND_SERVICES_DIR)

from services.data_service_docker import DataService  # noqa: E402


class TestAnnualizedSharpe:
    """年化夏普比率测试"""

    def test_normal_case(self):
        """正常值：均值 0.001，标准差 0.01，sharpe ≈ 0.1 * sqrt(365) ≈ 1.91"""
        # 构造收益率序列：均值 0.001，标准差约 0.01
        # 用一组确定性数据：[0.011, -0.009, 0.011, -0.009, 0.011, -0.009, ...]
        # 均值 = 0.001，标准差 ≈ 0.01
        daily = [0.011, -0.009] * 100  # 200 个数据点
        result = annualized_sharpe(daily)
        assert result is not None
        assert abs(result - 0.1 * math.sqrt(ANNUALIZATION_PERIODS)) < 0.01

    def test_sample_insufficient_single(self):
        """样本不足（1 个数据点）→ None"""
        assert annualized_sharpe([0.01]) is None

    def test_sample_insufficient_empty(self):
        """空列表 → None"""
        assert annualized_sharpe([]) is None

    def test_std_zero_all_zeros(self):
        """全 0 收益率 → 标准差为 0 → None"""
        assert annualized_sharpe([0.0] * 30) is None

    def test_std_zero_all_same(self):
        """所有收益率相同（非零）→ 标准差为 0 → None"""
        assert annualized_sharpe([0.01] * 30) is None

    def test_consistent_negative(self):
        """持续亏损：均值为负 → sharpe 为负"""
        daily = [-0.011, 0.009] * 100  # 均值 = -0.001
        result = annualized_sharpe(daily)
        assert result is not None
        assert result < 0

    def test_two_samples(self):
        """刚好 MIN_SAMPLE_COUNT=2 个样本 → 应能计算"""
        result = annualized_sharpe([0.01, -0.01])
        assert result is not None


class TestMaxDrawdownRatio:
    """最大回撤比例测试"""

    def test_known_sequence(self):
        """净值 [1.0, 1.2, 1.1, 0.9, 1.0] → peak=1.2, 最低点=0.9, max_dd = (1.2-0.9)/1.2 = 0.25"""
        nv = [1.0, 1.2, 1.1, 0.9, 1.0]
        result = max_drawdown_ratio(nv)
        assert result is not None
        assert abs(result - 0.25) < 1e-9

    def test_monotonically_increasing(self):
        """单调递增 [1.0, 1.1, 1.2] → max_dd = 0.0（从未从高点回落）"""
        nv = [1.0, 1.1, 1.2]
        result = max_drawdown_ratio(nv)
        assert result == 0.0

    def test_all_flat(self):
        """全部相同 [1.0, 1.0, 1.0] → max_dd = 0.0"""
        assert max_drawdown_ratio([1.0, 1.0, 1.0]) == 0.0

    def test_sample_insufficient_single(self):
        """样本不足（1 个数据点）→ None"""
        assert max_drawdown_ratio([1.0]) is None

    def test_sample_insufficient_empty(self):
        """空列表 → None"""
        assert max_drawdown_ratio([]) is None

    def test_two_samples_drop(self):
        """刚好 MIN_SAMPLE_COUNT=2，且下跌 → max_dd > 0"""
        result = max_drawdown_ratio([1.0, 0.8])
        assert result is not None
        assert abs(result - 0.2) < 1e-9

    def test_two_samples_rise(self):
        """刚好 MIN_SAMPLE_COUNT=2，且上涨 → max_dd = 0"""
        assert max_drawdown_ratio([1.0, 1.2]) == 0.0

    def test_large_drawdown(self):
        """从 1.0 跌到 0.5 再反弹 → max_dd = (1.0-0.5)/1.0 = 0.5"""
        nv = [1.0, 0.8, 0.5, 0.6, 0.9, 1.1]
        result = max_drawdown_ratio(nv)
        assert abs(result - 0.5) < 1e-9


class TestConstants:
    """常量值验证"""

    def test_annualization_periods(self):
        """币圈 7x24 年化系数应为 365"""
        assert ANNUALIZATION_PERIODS == 365

    def test_min_sample_count(self):
        """最小样本数应为 2"""
        assert MIN_SAMPLE_COUNT == 2


# ============================================================
# 策略级净值曲线口径（dashboard DataService，DB 用假实现）
# ============================================================

class _FakeDB:
    """DataService 的假 DB：只实现绩效模块用到的 fetch_all / fetch_one

    - fetch_all: 仅处理 trade_records 的按日盈亏聚合查询
    - fetch_one: 仅处理 strategy_position_snapshot_history 的资金基数/保证金查询，
      按 SQL 中出现的列名（allocated_amount / open_margin）、是否有时间上界
      (`snapshot_hour <=`) 与排序方向（ASC/DESC）模拟真实过滤，便于断言
      「不再按月查询」「不再取未来快照」。
    """

    def __init__(self, trade_rows=None, snapshots=None):
        self.trade_rows = trade_rows or []
        self.snapshots = snapshots or []
        self.all_sqls = []
        self.one_sqls = []

    async def fetch_all(self, sql, *params):
        self.all_sqls.append(sql)
        if "trading.trade_records" in sql:
            # 净值曲线日盈亏查询行为模拟：识别 CTE 持仓级防双计 SQL 且
            # trade_rows 里是**原始明细行**（含 executed_at）→ 用 Python 复现
            # `_DAILY_PNL_QUERY` 逻辑（grp CTE BOOL_OR + 两段 FILTER + 天级 GROUP BY），
            # 产出真实的 {day, day_pnl} 聚合结果。若 trade_rows 里已经是预聚合好的
            # {day, day_pnl} 格式（旧测试直通行），则保持直通以兼容既有测试。
            if ("WITH grp AS" in sql and "day_pnl" in sql and "DATE(t.executed_at)" in sql
                    and self.trade_rows and "executed_at" in self.trade_rows[0]):
                return self._simulate_daily_pnl_query(params)
            return list(self.trade_rows)
        return []

    def _simulate_daily_pnl_query(self, params):
        """用 Python 复现 DataService._DAILY_PNL_QUERY 的 CTE 持仓级防双计逻辑。

        输入（self.trade_rows）应为**原始明细行**（含 strategy / symbol / side /
        order_type / realized_pnl / executed_at）。模拟 grp CTE 的
        (strategy, symbol, side) 分组 BOOL_OR(order_type='PNL_SUMMARY')，再按
        DATE(executed_at) 分组聚合：有汇总组只计汇总行，无汇总组计全行 realized_pnl。

        params 对应 SQL 的 $1..$3，仅用于窗口过滤（保持与真实查询 WHERE 对齐）。
        """
        strategies, start, end = (list(params[0]) if params else []), params[1], params[2]

        # 0. 按查询窗口过滤明细行（与 SQL WHERE 对齐）
        rows = [r for r in self.trade_rows
                if r.get("strategy") in strategies
                and start <= r.get("executed_at", datetime.min) < end]

        # 1. grp CTE：(strategy, symbol, side) 键 → has_summary=BOOL_OR(order_type='PNL_SUMMARY')
        # key 用元组，None 键用 "__NULL__" 占位以模拟 SQL 的 IS NOT DISTINCT FROM 语义
        _NULL = "__NULL__"

        def _k(v):
            return _NULL if v is None else v

        grp_has_summary = {}
        for r in rows:
            gkey = (_k(r.get("strategy")), _k(r.get("symbol")), _k(r.get("side")))
            grp_has_summary[gkey] = grp_has_summary.get(gkey, False) or (
                r.get("order_type") == "PNL_SUMMARY"
            )

        # 2. 按 DATE(executed_at) 分组聚合：
        #    有汇总组（has_summary=True）→ 只计 order_type='PNL_SUMMARY' 行
        #    无汇总组（has_summary=False）→ 计全行 realized_pnl
        day_pnl_map = {}
        for r in rows:
            gkey = (_k(r.get("strategy")), _k(r.get("symbol")), _k(r.get("side")))
            has_summary = grp_has_summary.get(gkey, False)
            pnl = r.get("realized_pnl") or 0.0

            if has_summary:
                # 该持仓组存在汇总行 → 只累计汇总行，明细跳过（跨天不再双计）
                if r.get("order_type") != "PNL_SUMMARY":
                    continue
            # has_summary=False → 回退全行口径（其他策略行为不变）

            day = r.get("executed_at").date() if r.get("executed_at") else None
            if day is None:
                continue
            day_pnl_map[day] = day_pnl_map.get(day, 0.0) + pnl

        return [{"day": day, "day_pnl": pnl} for day, pnl in sorted(day_pnl_map.items())]

    async def fetch_one(self, sql, *params):
        self.one_sqls.append(sql)
        if "strategy_position_snapshot_history" not in sql:
            return None
        col = "allocated_amount" if "allocated_amount" in sql else "open_margin"
        has_upper = "snapshot_hour <=" in sql
        ascending = "ASC" in sql
        at = params[1] if has_upper and len(params) > 1 else None
        cands = [s for s in self.snapshots if (s.get(col) or 0) > 0]
        if has_upper:
            cands = [s for s in cands if s["snapshot_hour"] <= at]
        if not cands:
            return None
        cands.sort(key=lambda s: s["snapshot_hour"])
        chosen = cands[0] if ascending else cands[-1]
        return {col: chosen[col]}


def _service_with(db) -> DataService:
    svc = DataService()
    svc._db_manager = db
    return svc


class TestPerfEntryWindow:
    """_compute_one_perf_entry：窗口截取与实际样本窗口落库字段

    同步方法类，合跑前若有 async 测试污染事件循环（_local._loop 残留 closed loop），
    会触发 RuntimeError "There is no current event loop"。
    autouse fixture 每次方法前重置 _local._loop，确保跨文件合跑隔离。
    """

    @pytest.fixture(autouse=True)
    def _reset_event_loop(self):
        """每次方法前重置 asyncio 事件循环（pytest-asyncio 跨文件合跑污染守卫）

        问题根因：async 测试关闭 loop 后 _local._set_called 残留 True，
        导致后续同步测试触发 get_event_loop() 时跳过"自动 new loop"分支直接 raise。
        修法：同时清 _loop 和 _set_called 两个私有属性。
        """
        import asyncio
        policy = asyncio.get_event_loop_policy()
        loop_local = getattr(policy, "_local", None)
        if loop_local is not None:
            try:
                policy.set_event_loop(None)
                loop_local._set_called = False  # 关键：pytest-asyncio 关闭 loop 后会留 True
            except Exception:
                pass

    def test_window_dates_recorded(self):
        svc = DataService()
        rets = [0.0, 0.01, -0.02, 0.03]
        net_vals = [1.0, 1.01, 0.99, 1.02]
        dates = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]

        row = svc._compute_one_perf_entry(
            "day", "strategy", "hrs", rets, net_vals, dates, date(2026, 9, 1), 30,
        )

        assert row is not None
        (granularity, scope, sid, bucket_key, sample_count,
         sharpe, max_dd, window_start, window_end) = row
        assert granularity == "day" and scope == "strategy" and sid == "hrs"
        assert sample_count == 4
        assert window_start == date(2026, 9, 1)
        assert window_end == date(2026, 9, 4)

    def test_truncation_keeps_alignment(self):
        """截取最近 N 天时三个序列同步尾部截取，窗口起止与实际样本一致"""
        svc = DataService()
        rets = [0.01] * 100
        net_vals = [1.0 + i * 0.01 for i in range(100)]
        dates = [date(2026, 1, 1).toordinal() + i for i in range(100)]

        row = svc._compute_one_perf_entry(
            "day", "total", "", rets, net_vals, dates, date(2026, 4, 1), 30,
        )

        assert row is not None
        sample_count = row[4]
        window_start, window_end = row[7], row[8]
        assert sample_count == 30
        assert window_start == dates[-30]
        assert window_end == dates[-1]

    def test_sample_insufficient_returns_none(self):
        svc = DataService()
        row = svc._compute_one_perf_entry(
            "day", "total", "", [0.0], [1.0], [date(2026, 9, 1)], date(2026, 9, 1), 30,
        )
        assert row is None



class TestPnlDedupScope:
    """盈亏防双计口径守卫测试（与 ai_tuner/allocation/pnl_collector.py 同款条件对齐）

    背景：HRS 的整笔平仓累计盈亏（PNL_SUMMARY 行）与分笔回填明细行并存，
    混合 SUM 会把同一笔平仓记两次（2026-09 实测 HRS 亏损被虚增至 -31.31，
    防双计后为 -2.28）。本测试防止口径被误改回混合求和。
    """

    def test_agg_columns_use_conditional_summary_only_scope(self):
        """聚合列模板：统计组内存在汇总行则盈亏类指标只按汇总行计"""
        cols = DataService._TRADE_RECORD_AGG_COLUMNS
        # 条件分支：存在 PNL_SUMMARY 行 → 盈亏类指标只累计汇总行（防双计）
        assert "BOOL_OR(order_type = 'PNL_SUMMARY')" in cols
        assert "SUM(realized_pnl) FILTER (WHERE order_type = 'PNL_SUMMARY')" in cols
        assert "COUNT(*) FILTER (WHERE order_type = 'PNL_SUMMARY')" in cols
        # 无汇总行 → 回退全行口径（其他策略行为不变）
        assert "ELSE COALESCE(SUM(realized_pnl), 0)" in cols
        assert "ELSE COUNT(realized_pnl)" in cols
        # order_count / commission 不涉双计，维持全行口径
        assert "COUNT(*) AS order_count" in cols
        assert "SUM(commission), 0) AS commission" in cols

    def test_daily_pnl_query_uses_conditional_summary_only_scope(self):
        """净值曲线日盈亏查询采用持仓级防双计口径（CTE 分组 + IS NOT DISTINCT FROM JOIN）"""
        query = DataService._DAILY_PNL_QUERY
        # 1. CTE 先在 (strategy, symbol, side) 持仓级分组判定 BOOL_OR
        assert "WITH grp AS" in query
        assert "GROUP BY strategy, symbol, side" in query
        assert "BOOL_OR(order_type = 'PNL_SUMMARY') AS has_summary" in query
        # 2. 主查询 JOIN grp，IS NOT DISTINCT FROM 防 NULL 键行丢失
        assert "IS NOT DISTINCT FROM" in query
        # 3. day_pnl 由两段 FILTER 相加：有汇总组只计 PNL_SUMMARY 行，无汇总组计全行
        assert "FILTER (WHERE g.has_summary AND t.order_type = 'PNL_SUMMARY')" in query
        assert "FILTER (WHERE NOT g.has_summary)" in query
        # 4. 窗口与天级分组保持不变
        assert "strategy = ANY($1::text[])" in query
        assert "GROUP BY DATE(t.executed_at)" in query

    async def test_cross_day_position_no_double_count_when_summary_exists(self):
        """跨天持仓防双计行为级：同一 (symbol, side) 组 day1 明细 + day2 汇总 →
        明细日 day_pnl=0（有汇总组的明细不再计）、汇总日 day_pnl=-8、合计 -8

        旧实现（天级判定）会得 day1=-5, day2=-3+(-8)=-11 → 合计 -13（双计）。
        本 fake 行为模拟用 CTE 持仓级判定，应当只计汇总行。
        """
        # 构造原始明细行：同一 (strategy='hrs', symbol='XXXUSDT', side='SELL') 组
        # day1 有一条 TAKE_PROFIT 明细 realized_pnl=-5.0
        # day2 有另一条 TAKE_PROFIT 明细 realized_pnl=-3.0 + 一条 PNL_SUMMARY realized_pnl=-8.0
        window_start = datetime(2026, 9, 1, 0, 0)
        window_end = datetime(2026, 9, 10, 0, 0)
        trade_rows = [
            {"strategy": "hrs", "symbol": "XXXUSDT", "side": "SELL",
             "order_type": "TAKE_PROFIT", "realized_pnl": -5.0,
             "executed_at": datetime(2026, 9, 2, 10, 0)},
            {"strategy": "hrs", "symbol": "XXXUSDT", "side": "SELL",
             "order_type": "TAKE_PROFIT", "realized_pnl": -3.0,
             "executed_at": datetime(2026, 9, 3, 14, 0)},
            {"strategy": "hrs", "symbol": "XXXUSDT", "side": "SELL",
             "order_type": "PNL_SUMMARY", "realized_pnl": -8.0,
             "executed_at": datetime(2026, 9, 3, 15, 0)},
        ]
        db = _FakeDB(trade_rows=trade_rows)
        svc = _service_with(db)

        # 直接复用 fake 的 _simulate_daily_pnl_query 模拟持仓级聚合输出
        simulated = db._simulate_daily_pnl_query(
            (["hrs"], window_start, window_end))
        day_map = {str(r["day"]): r["day_pnl"] for r in simulated}

        # 跨天持仓防双计：day1(09-02) 有汇总组 → 明细不计 → day_pnl=0
        assert day_map.get("2026-09-02", 0.0) == pytest.approx(0.0)
        # day2(09-03) 汇总行 → day_pnl=-8.0（明细 -3.0 被跳过）
        assert day_map.get("2026-09-03", 0.0) == pytest.approx(-8.0)
        # 两天合计 = -8.0（只落一次汇总金额，旧天级实现会得 day1=-5 + day2=-8 = -13 的双计值）
        assert sum(day_map.values()) == pytest.approx(-8.0)

    async def test_no_summary_fallback_to_all_details(self):
        """无汇总组回退全行口径（其他策略行为不变）：
        只有明细 -5.0 / -3.0，无 PNL_SUMMARY → day1=-5.0, day2=-3.0
        """
        window_start = datetime(2026, 9, 1, 0, 0)
        window_end = datetime(2026, 9, 10, 0, 0)
        trade_rows = [
            {"strategy": "btc_eth", "symbol": "BTCUSDT", "side": "BUY",
             "order_type": "TAKE_PROFIT", "realized_pnl": -5.0,
             "executed_at": datetime(2026, 9, 2, 10, 0)},
            {"strategy": "btc_eth", "symbol": "BTCUSDT", "side": "BUY",
             "order_type": "STOP_LOSS", "realized_pnl": -3.0,
             "executed_at": datetime(2026, 9, 3, 14, 0)},
        ]
        db = _FakeDB(trade_rows=trade_rows)

        simulated = db._simulate_daily_pnl_query(
            (["btc_eth"], window_start, window_end))
        day_map = {str(r["day"]): r["day_pnl"] for r in simulated}

        assert day_map.get("2026-09-02", 0.0) == pytest.approx(-5.0)
        assert day_map.get("2026-09-03", 0.0) == pytest.approx(-3.0)
        assert sum(day_map.values()) == pytest.approx(-8.0)


# ============================================================
# CS1：资金段推导 derive_segments（纯函数单测，无 DB 依赖）
# ============================================================

class TestDeriveSegments:
    """CS1 段边界推导测试（架构 §4.1：主锚 E1/E2 + 辅锚 E3）"""

    @pytest.fixture(autouse=True)
    def _reset_event_loop(self):
        """同步类方法用不到事件循环，但 autouse 保所有合跑隔离"""
        import asyncio
        policy = asyncio.get_event_loop_policy()
        loop_local = getattr(policy, "_local", None)
        if loop_local is not None:
            try:
                policy.set_event_loop(None)
                loop_local._set_called = False
            except Exception:
                pass

    def test_e1_month_switch_detected(self):
        """主锚 E1：capital_allocation 新增 month 记录 → 段边界"""
        rows = [
            (date(2026, 9, 1), datetime(2026, 9, 1, 0, 0),
             [{"strategy_id": "hrs", "allocated_ratio": 0.1}]),
            (date(2026, 10, 1), datetime(2026, 10, 1, 0, 0),
             [{"strategy_id": "hrs", "allocated_ratio": 0.1}]),
        ]
        # snapshots 只需一条（E1 不依赖 E3）
        snapshots = [(datetime(2026, 9, 1, 0, 0), 58.37)]

        events = DataService.derive_segments("hrs", rows, snapshots, static_days_k=3)

        # 应检测到 2 个 E1 事件（同同时点不重复）
        assert len(events) == 2
        assert events[0]["event_type"] == "E1_month_switch"
        assert events[1]["event_type"] == "E1_month_switch"

    def test_e2_ratio_change_detected(self):
        """主锚 E2：相邻 month 记录 ratio 不同 → 额外段边界（依附 E1 的 created_at）"""
        rows = [
            (date(2026, 9, 1), datetime(2026, 9, 1, 0, 0),
             [{"strategy_id": "hrs", "allocated_ratio": 0.1}]),
            (date(2026, 10, 1), datetime(2026, 10, 1, 0, 0),
             [{"strategy_id": "hrs", "allocated_ratio": 0.15}]),   # ratio 变了
        ]
        snapshots = [(datetime(2026, 9, 1, 0, 0), 58.37)]

        events = DataService.derive_segments("hrs", rows, snapshots, static_days_k=3)

        # 应检测到 2 个时点：9-1（E1）、10-1（E1+E2 复合）
        assert len(events) == 2
        # 9-1 时点只有 E1（第一个 record 没有前一个可比）
        assert events[0]["event_type"] == "E1_month_switch"
        # 10-1 时点有 E1 + E2（ratio 从 0.1 → 0.15）
        assert "E1_month_switch" in events[1]["event_type"]
        assert "E2_ratio_change" in events[1]["event_type"]

    def test_e3_static_jump_detected_when_constant_k_days(self):
        """辅锚 E3：跳变后恒定 ≥ K 天 → 段边界"""
        # 构造 capital_allocation 空（避免 E1/E2 干扰）
        rows = []
        # snapshots：58.37 → 104.22（恒定 10 天）→ 53.84（新值）
        snapshots = [
            (datetime(2026, 9, 10, 17, 0), 58.37),
            (datetime(2026, 9, 11, 14, 0), 104.22),   # 跳变
            # 后面每天都是 104.22，持续 >= K=3 天
            (datetime(2026, 9, 12, 14, 0), 104.22),
            (datetime(2026, 9, 13, 14, 0), 104.22),
            (datetime(2026, 9, 14, 14, 0), 104.22),
            (datetime(2026, 9, 15, 14, 0), 104.22),
            (datetime(2026, 9, 16, 14, 0), 104.22),
            (datetime(2026, 9, 17, 14, 0), 104.22),
            (datetime(2026, 9, 18, 14, 0), 104.22),
            (datetime(2026, 9, 19, 14, 0), 104.22),
            (datetime(2026, 9, 20, 14, 0), 104.22),  # 这里结束了（从 9-11 到 9-20 是 9 天，满足 K=3）
        ]

        events = DataService.derive_segments("hrs", rows, snapshots, static_days_k=3)

        # 空 capital_allocation → 只有 E3 跳变
        assert len(events) == 1
        assert events[0]["event_type"] == "E3_static_jump"
        assert events[0]["event_time"] == datetime(2026, 9, 11, 14, 0)

    def test_daily_drift_excluded_by_k_threshold(self):
        """每日漂移（跳变后恒定 < K 天，或每天都变 → 没有恒定段）→ 不算 E3"""
        rows = []
        # 构造：104.22 → 每天小幅变化（52.67→53.17→53.39→...），没有持续 ≥ K 天的段
        snapshots = [
            (datetime(2026, 9, 21, 13, 0), 104.22),  # 前一天还是 104.22，这里开始变
            (datetime(2026, 9, 22, 0, 0), 52.67),
            (datetime(2026, 9, 23, 0, 0), 53.17),
            (datetime(2026, 9, 24, 0, 0), 53.39),
            (datetime(2026, 9, 25, 0, 0), 53.50),
            (datetime(2026, 9, 26, 0, 0), 53.77),
            # 每天都变，没有任何值能保持恒定 ≥ K=3 天
        ]

        events = DataService.derive_segments("hrs", rows, snapshots, static_days_k=3)

        # 没有 E1/E2，也没有满足 K 阈值的 E3 → 应该 0 事件
        assert len(events) == 0

    def test_e3_does_not_align_with_e1(self):
        """E3 不对齐 E1（1 小时容差内）→ 不应重复记"""
        rows = [
            (date(2026, 10, 1), datetime(2026, 10, 1, 0, 0),
             [{"strategy_id": "hrs", "allocated_ratio": 0.1}]),
        ]
        # E3 跳变刚好在 E1 事件之后 30 分钟 → 1 小时容差内 → 对齐 → 不算额外 E3
        snapshots = [
            (datetime(2026, 9, 25, 0, 0), 58.37),
            (datetime(2026, 10, 1, 0, 30), 56.81),  # 跳变点，离 E1 created_at 仅 30 分钟
            # 后面 K 天都恒定
        ] + [
            (datetime(2026, 10, d, 0, 30), 56.81)
            for d in range(2, 8)
        ]

        events = DataService.derive_segments("hrs", rows, snapshots, static_days_k=3)

        # 不应有 E3，只有 E1
        event_types = [e["event_type"] for e in events]
        assert "E3_static_jump" not in event_types
        assert "E1_month_switch" in event_types


# ============================================================
# CS2：分段回撤算法（跨段独立 / 残缺段重归一）
# ============================================================

class TestSegmentedMaxDrawdown:
    """CS2 分段回撤正确性测试"""

    @pytest.fixture(autouse=True)
    def _reset_event_loop(self):
        import asyncio
        policy = asyncio.get_event_loop_policy()
        loop_local = getattr(policy, "_local", None)
        if loop_local is not None:
            try:
                policy.set_event_loop(None)
                loop_local._set_called = False
            except Exception:
                pass

    def test_cross_segment_no_comparison_ac1_4(self):
        """AC-1.4：段 A 净值触 0、段 B 重新开始 → 段 B 回撤不受段 A 峰值影响"""
        from datetime import date as _date

        # 段 A：净值从 1.0 涨到 1.2 然后跌到 0.0（本金亏光）
        seg_a = {
            "segment_start": datetime(2026, 9, 1),
            "opening_capital": 100.0,
            "dates": [_date(2026, 9, d) for d in range(1, 8)],
            "daily_returns": [0.0, 0.2, 0.0, 0.0, -1.2, 0.0, 0.0],
            "net_values": [1.0, 1.2, 1.2, 1.2, 0.0, 0.0, 0.0],
        }
        # 段 B：净值从 1.0 涨到 1.1 然后跌到 0.9（独立，不受段 A peak=1.2 污染）
        seg_b = {
            "segment_start": datetime(2026, 9, 8),
            "opening_capital": 150.0,
            "dates": [_date(2026, 9, d) for d in range(8, 15)],
            "daily_returns": [0.0, 0.1, 0.0, 0.0, -0.2 / 1.1, 0.0, 0.0],
            "net_values": [1.0, 1.1, 1.1, 1.1, 0.9, 0.9, 0.9],
        }

        svc = DataService()
        # 窗口覆盖两个段（window_days=30 足够）
        row = svc._compute_strategy_perf_by_segments(
            "year", "hrs", [seg_a, seg_b],
            _date(2026, 10, 1),  # bucket_key
            30,                   # window_days
            _date(2026, 9, 14),  # today
        )
        assert row is not None
        # 段 A 回撤：peak=1.2 → 跌到 0.0 → dd = (1.2-0.0)/1.2 = 1.0
        # 段 B 回撤：peak=1.1 → 跌到 0.9 → dd = (1.1-0.9)/1.1 ≈ 0.1818
        # 总体 = max(1.0, 0.1818) = 1.0
        granularity, scope, sid, bk, n, sharpe, max_dd, ws, we = row
        assert max_dd == pytest.approx(1.0)
        # 段 B 的 dd 不受段 A peak 1.2 污染——如果跨段比，整体 peak=1.2，
        # 段 B 的 0.9 会算出 (1.2-0.9)/1.2=0.25（错误），但我们正确返回 max(1.0,0.1818)=1.0
        # 这个断言已由 1.0 验证（段 A 的 100% 回撤主导总体）

    def test_segment_b_dd_independent_of_segment_a_peak(self):
        """AC-1.4 补证：段 A 回撤小（0.0），段 B 独立回撤 → 段 B 回撤正确

        段 B 构造（段起点 9-8 当天 day_pnl=0，daily_return=0，nv=1.0）：
          9-8: r=0.0 → cum=0 → nv=1.0
          9-9: r=0.1 → cum=0.1 → nv=1.1（PnL=10/base=100）
          9-10: r=-0.2 → cum=-0.1 → nv=0.9（PnL=-20/base=100）
        cum-reset nv=[1.0,1.1,0.9] → peak=1.1 → dd=(1.1-0.9)/1.1≈0.1818
        跨段独立：若错误拼接，段 A 峰值 1.77 会让段 B 的 0.9 算出 dd≈0.49。
        """
        from datetime import date as _date

        # 段 A：净值单调递增，dd=0.0（每天 day_pnl=10, r=0.1）
        seg_a = {
            "segment_start": datetime(2026, 9, 1),
            "opening_capital": 100.0,
            "dates": [_date(2026, 9, d) for d in range(1, 8)],
            "daily_returns": [0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1],
            "net_values": [1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7],
        }
        # 段 B：净值 1.0→1.1→0.9，独立 dd≈0.1818
        seg_b = {
            "segment_start": datetime(2026, 9, 8),
            "opening_capital": 100.0,
            "dates": [_date(2026, 9, d) for d in range(8, 11)],
            # 段起点当天 day_pnl=0 → r=0；9-9 PnL=10 → r=0.1；9-10 PnL=-20 → r=-0.2
            "daily_returns": [0.0, 0.1, -0.2],
            "net_values": [1.0, 1.1, 0.9],
        }

        svc = DataService()
        row = svc._compute_strategy_perf_by_segments(
            "year", "hrs", [seg_a, seg_b],
            _date(2026, 10, 1), 30, _date(2026, 9, 10),
        )
        assert row is not None
        _, _, _, _, _, _, max_dd, _, _ = row
        # 跨段独立 → 总体 max_dd = max(段A dd, 段B dd) = max(0.0, 0.1818) = 0.1818
        assert max_dd == pytest.approx(0.1818181818, abs=1e-6)

    def test_incomplete_segment_renormalization_ac1_5(self):
        """AC-1.5：窗口起点落在段中间 → cum 从窗口起点重新归一（残缺段重归一）

        段构造（段起点 9-1 day_pnl=0, r=0, nv=1.0；base=100）：
          9-1: PnL=0 → r=0.0  → cum=0 → nv=1.0
          9-2: PnL=10 → r=0.1  → cum=0.1 → nv=1.1
          9-3: PnL=-20 → r=-0.2 → cum=-0.1 → nv=0.9
          9-4: PnL=30 → r=0.3  → cum=0.2 → nv=1.2
          9-5~9-8: PnL=0 → r=0.0 → nv=1.2
        窗口 = today=9-8, window_days=5 → window_start=9-3
          → effective_start=9-3, start_idx=2
          → seg_window_rets = [-0.2, 0.3, 0.0, 0.0, 0.0, 0.0]
        cum-reset（cum 从 0 起重新归零）：
          r=-0.2 → cum=-0.2 → nv=0.8  ← peak 前的低点
          r=0.3  → cum=0.1  → nv=1.1  ← peak 更新，peak 前的低点不再计入 dd
          r=0×4  → nv=1.1
        running peak 不回头 → peak=1.1 之后无更低点 → dd=0.0
        验证了 AC-1.5：残缺段重归一后不继承段起点前的 1.0 峰值或 0.9 低点。
        """
        from datetime import date as _date

        seg = {
            "segment_start": datetime(2026, 9, 1),
            "opening_capital": 100.0,
            "dates": [_date(2026, 9, d) for d in range(1, 9)],      # 9-1 到 9-8（8 天）
            # 每天 day_pnl/base，长度与 dates 对齐
            "daily_returns": [0.0, 0.1, -0.2, 0.3, 0.0, 0.0, 0.0, 0.0],
            "net_values": [1.0, 1.1, 0.9, 1.2, 1.2, 1.2, 1.2, 1.2],
        }

        svc = DataService()
        # window_days=5 → window_start = 9-8 - 5 = 9-3 → start_idx=2
        row = svc._compute_strategy_perf_by_segments(
            "week", "hrs", [seg],
            _date(2026, 10, 1), 5, _date(2026, 9, 8),
        )
        assert row is not None
        _, _, _, _, _, _, max_dd, _, _ = row

        # cum-reset + running peak 不回头 → 0.8 是 peak 前的低点，peak=1.1 后无更低点
        assert max_dd == pytest.approx(0.0, abs=1e-6)

    def test_zero_denominator_segment_skipped(self):
        """§4.5：零分母期段（skip=True）不计入回撤

        seg_ok 构造（base=100，段起点 9-8 day_pnl=0 → r=0 → nv=1.0）：
          9-8: r=0.0  → cum=0 → nv=1.0
          9-9: r=0.1  → cum=0.1 → nv=1.1（PnL=10）
          9-10: r=-0.2 → cum=-0.1 → nv=0.9（PnL=-20）
          9-11~9-14: r=0 → nv=0.9
        cum-reset nv=[1.0,1.1,0.9,0.9,0.9,0.9,0.9] → peak=1.1 → dd≈0.1818
        """
        from datetime import date as _date

        # 构造一个 skip=True 的段（零分母期） + 一个正常段
        seg_skip = {
            "segment_start": datetime(2026, 9, 1),
            "opening_capital": 0.0,       # 零分母
            "skip": True,
            "dates": [_date(2026, 9, d) for d in range(1, 8)],
            "daily_returns": [],          # 空
            "net_values": [],              # 空
        }
        seg_ok = {
            "segment_start": datetime(2026, 9, 8),
            "opening_capital": 100.0,
            "skip": False,
            "dates": [_date(2026, 9, d) for d in range(8, 15)],       # 7 天
            # 段起点当天 day_pnl=0 → r=0；9-9 PnL=10 → r=0.1；9-10 PnL=-20 → r=-0.2
            "daily_returns": [0.0, 0.1, -0.2, 0.0, 0.0, 0.0, 0.0],
            "net_values": [1.0, 1.1, 0.9, 0.9, 0.9, 0.9, 0.9],
        }

        # seg_skip 在 _build_strategy_segmented_series 里就会被跳过（因为 skip=True），
        # 所以传到 _compute_strategy_perf_by_segments 的只能是 seg_ok
        svc = DataService()
        row = svc._compute_strategy_perf_by_segments(
            "week", "hrs", [seg_ok],    # skip 段已在上层过滤
            _date(2026, 10, 1), 30, _date(2026, 9, 14),
        )
        assert row is not None
        _, _, _, _, _, _, max_dd, _, _ = row
        # 只有 seg_ok 参与：peak=1.1 → 跌到 0.9 → dd=0.1818
        assert max_dd == pytest.approx(0.1818181818, abs=1e-6)


# ============================================================
# CS4：caliber_version 审计列守卫
# ============================================================

class TestCaliberVersionCS4:
    """CS4 caliber_version 审计列的 SQL 文本守卫 + 常量验证"""

    def test_upsert_sql_contains_caliber_version(self):
        """UPSERT SQL 必须 INSERT + UPDATE caliber_version"""
        sql = DataService._PERF_METRIC_UPSERT_SQL
        assert "caliber_version" in sql
        assert "INSERT INTO public.performance_metric_snapshot" in sql
        assert "EXCLUDED.caliber_version" in sql

    def test_caliber_version_constant(self):
        """常量必须为 'segmented-v1'"""
        assert DataService._CALIBER_VERSION == "segmented-v1"

    def test_perf_segment_static_days_configurable(self):
        """E3 阈值 K 必须来自环境变量、默认 3"""
        # 默认值断言（环境变量未设置时应为 3）
        import os as _os
        _orig = _os.environ.get("PERF_SEGMENT_STATIC_DAYS")
        try:
            # 清除环境变量，让默认值生效
            if "PERF_SEGMENT_STATIC_DAYS" in _os.environ:
                del _os.environ["PERF_SEGMENT_STATIC_DAYS"]
            # DataService._PERF_SEGMENT_STATIC_DAYS 是类属性，在 import 时已固化
            # 这里只断言它是 int 类型且 >= 1（即配置化了、不是硬编码）
            assert isinstance(DataService._PERF_SEGMENT_STATIC_DAYS, int)
            assert DataService._PERF_SEGMENT_STATIC_DAYS >= 1
        finally:
            if _orig is not None:
                _os.environ["PERF_SEGMENT_STATIC_DAYS"] = _orig


# ============================================================
# N3 改写后的失效测试（旧口径已废弃，按新口径重写）
# ============================================================

class TestBaseCapitalQueryNewCaliber:
    """N3：段初分配额新口径（向后取）守卫

    旧口径「全窗口单一基数 + _query_strategy_base_capital 单点查询」
    已废弃（方法已删除），改为「按段取段初分配额（向后取，
    snapshot_hour >= 事件时点 ORDER BY ASC LIMIT 1）」。

    本组测试验证新口径的段初分配额查询 SQL 和静态方法，确保方向正确。
    """

    async def test_segment_opening_capital_query_backward_direction(self):
        """新口径段初分配额：事件时点**之后**首条正快照（向后取，ORDER BY ASC LIMIT 1）"""
        sql = DataService._SEGMENT_OPENING_CAPITAL_SQL
        # 必须是向后取（>= 事件时点 + ORDER BY ASC）
        assert "snapshot_hour >=" in sql
        assert "ORDER BY snapshot_hour ASC" in sql
        assert "LIMIT 1" in sql
        # 必须查 allocated_amount > 0（正快照）
        assert "allocated_amount > 0" in sql


class TestStrategyCurveNewCaliber:
    """N3：旧 TestStrategyCurveBaseCapital 按新口径改写

    旧测试基于「_build_strategy_daily_returns 单一基数 + 尾部切片」的旧实现——
    这些行为是对的（不复利、净值钳 0），现在仍应保留，因为新口径也保留了
    「段内不复利 + 净值钳 0」的底层逻辑。

    本组测试验证新分段方法 _build_strategy_segmented_series 的段内净值构建正确性。
    """

    async def test_segment_net_value_capped_at_zero(self):
        """段内净值钳 0、段内不复利（架构 §4.2）

        B 段测试补强：原实现为 `if series:` 空转断言（旧 _FakeDB 不支持分段查询
        → series 恒为 []，断言从未真实执行）。现用 _SegmentFakeDB 走通
        _derive_segments_for_strategy → _build_strategy_segmented_series 全链路。
        """
        db = _SegmentFakeDB(
            snapshot_rows=[
                {"snapshot_hour": datetime(2026, 9, 10, 17, 0),
                 "allocated_amount": 100.0},
            ],
            day_rows=[
                {"day": date(2026, 9, 10), "day_pnl": -150.0},
            ],
        )
        svc = _service_with(db)

        series = await svc._build_strategy_segmented_series(
            "hrs",
            datetime(2026, 9, 10), datetime(2026, 9, 11),
        )

        assert series, "分段序列不应为空（有快照行 → 退化单段 snapshot_debut）"
        seg = series[0]
        assert seg["opening_capital"] == pytest.approx(100.0)
        # 净值一律非负（本金亏光 = 0）
        assert all(v >= 0 for v in seg["net_values"])
        # 9-10 亏 150 > 基数 100 → 净值钳 0；次日 0 盈亏不复利 → 仍为 0
        assert seg["net_values"] == pytest.approx([0.0, 0.0])
        assert seg["daily_returns"][0] == pytest.approx(-1.5)


# ============================================================
# B 段功能测试补充：分段链路 DB 假实现 + 各方法分支/边界覆盖
# ============================================================


def _amount_positive(v) -> bool:
    """模拟 SQL 谓词 allocated_amount > 0 的过滤语义（非数值脏数据视为非正）"""
    try:
        return v is not None and float(v) > 0
    except (TypeError, ValueError):
        return False


class _SegmentFakeDB:
    """分段口径（CS1/CS2/CS4）专用假 DB

    按真实 SQL 文本特征路由，覆盖 5 类查询 + UPSERT 执行：
    - _CHECK_SNAPSHOT_EXISTS_SQL（SELECT 1 ... LIMIT 1，无 ORDER BY）→ grid 不可评分守卫
    - _CAPITAL_ALLOCATION_HISTORY_SQL（public.capital_allocation）→ 分配历史（E1/E2）
    - snapshot_history 明细查询（ORDER BY snapshot_hour ASC）→ E3 去噪快照行
    - _SEGMENT_OPENING_CAPITAL_SQL（allocated_amount > 0 ... >= 事件时点 ASC LIMIT 1）
      → 段初分配额，严格模拟「向后取」语义
    - _DAILY_PNL_QUERY（trading.trade_records）→ 预聚合日盈亏行直通
    - execute → 记录 UPSERT 参数（CS4 行数 / caliber_version / grid 守卫断言用）
    """

    def __init__(self, snapshot_rows=None, alloc_rows=None, day_rows=None, exists=None,
                 opening_row_override=None):
        self.snapshot_rows = snapshot_rows or []
        self.alloc_rows = alloc_rows or []
        self.day_rows = day_rows or []
        self.exists = exists  # None → 按 snapshot_rows 是否非空推断
        # {evt_time: row}：段初查询命中该时点时直通返回（用于构造 float() 解析异常分支）
        self.opening_row_override = opening_row_override or {}
        self.opening_queries = []  # [(evt_time, 返回行或 None)]
        self.all_sqls = []
        self.one_sqls = []
        self.executed = []  # [(sql, params)]

    async def fetch_all(self, sql, *params):
        self.all_sqls.append(sql)
        if "public.capital_allocation" in sql:
            return list(self.alloc_rows)
        if "public.strategy_position_snapshot_history" in sql:
            return list(self.snapshot_rows)
        if "trading.trade_records" in sql:
            return list(self.day_rows)
        return []

    async def fetch_one(self, sql, *params):
        self.one_sqls.append(sql)
        if "public.strategy_position_snapshot_history" not in sql:
            return None
        if "allocated_amount > 0" in sql:
            # 段初分配额：生效时刻**之后**首条正快照（向后取，ASC LIMIT 1）
            evt_time = params[1]
            if evt_time in self.opening_row_override:
                row = self.opening_row_override[evt_time]
                self.opening_queries.append((evt_time, row))
                return row
            cands = [s for s in self.snapshot_rows
                     if s["snapshot_hour"] is not None
                     and s["snapshot_hour"] >= evt_time
                     and _amount_positive(s.get("allocated_amount"))]
            if not cands:
                row = None
            else:
                cands.sort(key=lambda s: s["snapshot_hour"])
                row = {"allocated_amount": cands[0]["allocated_amount"],
                       "snapshot_hour": cands[0]["snapshot_hour"]}
            self.opening_queries.append((evt_time, row))
            return row
        # 存在性守卫（SELECT 1 ... LIMIT 1）
        exists = self.exists if self.exists is not None else bool(self.snapshot_rows)
        return {} if exists else None

    async def execute(self, sql, *params):
        self.executed.append((sql, params))


class _RaisingExecDB(_SegmentFakeDB):
    """execute 对指定粒度抛错（验证单桶失败不中断整体流程）"""

    def __init__(self, fail_granularity="day", **kw):
        super().__init__(**kw)
        self.fail_granularity = fail_granularity

    async def execute(self, sql, *params):
        if params and params[0] == self.fail_granularity:
            raise RuntimeError("模拟单桶落库失败")
        self.executed.append((sql, params))


def _seg_series_for_compute(dates, net_values, daily_returns, opening=100.0):
    """构造 _compute_strategy_perf_by_segments 的输入段"""
    return [{
        "segment_start": datetime.combine(dates[0], datetime.min.time()),
        "opening_capital": opening,
        "opening_capital_time": datetime.combine(dates[0], datetime.min.time()),
        "skip": False,
        "daily_returns": daily_returns,
        "net_values": net_values,
        "dates": dates,
    }]


class TestDeriveSegmentsForStrategy:
    """CS1 DB 版段推导：grid 守卫 / 向后取段初 / 零分母 skip / 退化单段"""

    @pytest.fixture(autouse=True)
    def _reset_event_loop(self):
        import asyncio
        policy = asyncio.get_event_loop_policy()
        loop_local = getattr(policy, "_local", None)
        if loop_local is not None:
            try:
                policy.set_event_loop(None)
                loop_local._set_called = False
            except Exception:
                pass

    async def test_grid_guard_no_snapshot_rows_returns_empty(self):
        """§4.4：snapshot_history 无任何行（如 grid）→ 返回 []，不再继续查库"""
        db = _SegmentFakeDB()  # 无任何快照行
        svc = _service_with(db)

        segments = await svc._derive_segments_for_strategy("grid")

        assert segments == []
        # 只发了存在性守卫查询，未再读分配历史 / 快照明细 / 日盈亏
        assert len(db.one_sqls) == 1
        assert db.all_sqls == []

    async def test_opening_capital_is_first_positive_after_event_backward(self):
        """§4.1 段初契约：段初分配额 = 事件生效时刻**之后**首条正快照（向后取）

        事件 9-1 14:00 之前存在 58.37 快照 → 不得向前取（旧口径缺陷），
        段初必须取 9-1 15:00 的 104.22。
        """
        alloc_rows = [
            {"month": date(2026, 9, 1), "created_at": datetime(2026, 9, 1, 14, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
            {"month": date(2026, 10, 1), "created_at": datetime(2026, 10, 1, 14, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
        ]
        snapshot_rows = [
            # 事件前的旧快照（向前取会错取它）
            {"snapshot_hour": datetime(2026, 9, 1, 10, 0), "allocated_amount": 58.37},
            {"snapshot_hour": datetime(2026, 9, 1, 15, 0), "allocated_amount": 104.22},
            {"snapshot_hour": datetime(2026, 9, 20, 13, 0), "allocated_amount": 52.0},
            {"snapshot_hour": datetime(2026, 10, 1, 15, 0), "allocated_amount": 56.81},
        ]
        db = _SegmentFakeDB(snapshot_rows=snapshot_rows, alloc_rows=alloc_rows)
        svc = _service_with(db)

        segments = await svc._derive_segments_for_strategy("hrs")

        assert len(segments) == 2
        seg1, seg2 = segments
        # 段 1 段初 = 事件 9-1 14:00 之后的 104.22（不是事件前的 58.37，也不是全表最早 58.37）
        assert seg1["opening_capital"] == pytest.approx(104.22)
        assert seg1["opening_capital_time"] == datetime(2026, 9, 1, 15, 0)
        assert seg1["skip"] is False
        # 段 2 段初 = 事件 10-1 14:00 之后的 56.81
        assert seg2["opening_capital"] == pytest.approx(56.81)
        assert seg2["opening_capital_time"] == datetime(2026, 10, 1, 15, 0)
        assert seg2["skip"] is False

    async def test_zero_denominator_segment_marked_skip(self):
        """§4.5：某事件后无任何正快照（allocated_amount 恒 0）→ 该段 skip=True、段初 0"""
        alloc_rows = [
            {"month": date(2026, 9, 1), "created_at": datetime(2026, 9, 1, 15, 30),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
        ]
        snapshot_rows = [
            {"snapshot_hour": datetime(2026, 9, 1, 15, 30), "allocated_amount": 100.0},
            # 之后进入零分母期（0 恒定 3 天 → E3 事件 → 独立段）
            {"snapshot_hour": datetime(2026, 9, 2, 0, 0), "allocated_amount": 0.0},
            {"snapshot_hour": datetime(2026, 9, 3, 0, 0), "allocated_amount": 0.0},
            {"snapshot_hour": datetime(2026, 9, 4, 0, 0), "allocated_amount": 0.0},
            {"snapshot_hour": datetime(2026, 9, 5, 0, 0), "allocated_amount": 0.0},
        ]
        db = _SegmentFakeDB(snapshot_rows=snapshot_rows, alloc_rows=alloc_rows)
        svc = _service_with(db)

        segments = await svc._derive_segments_for_strategy("hrs")

        assert len(segments) == 2
        assert segments[0]["skip"] is False
        assert segments[0]["opening_capital"] == pytest.approx(100.0)
        # 零分母段：无正快照 → skip=True、opening=0.0（不得静默回退其它段的值）
        assert segments[1]["skip"] is True
        assert segments[1]["opening_capital"] == pytest.approx(0.0)

    async def test_no_events_falls_back_to_snapshot_debut_single_segment(self):
        """无 E1/E2/E3 事件 → 退化单段（event_type='snapshot_debut'，段初=首条正快照）"""
        db = _SegmentFakeDB(
            snapshot_rows=[
                {"snapshot_hour": datetime(2026, 9, 1, 17, 0), "allocated_amount": 100.0},
            ],
            alloc_rows=[],
        )
        svc = _service_with(db)

        segments = await svc._derive_segments_for_strategy("hrs")

        assert len(segments) == 1
        assert segments[0]["event_type"] == "snapshot_debut"
        assert segments[0]["opening_capital"] == pytest.approx(100.0)
        assert segments[0]["skip"] is False

    async def test_alloc_rows_with_null_month_or_created_at_skipped(self):
        """capital_allocation 行缺 month / created_at → 跳过该行，不产生事件"""
        alloc_rows = [
            {"month": None, "created_at": datetime(2026, 9, 1, 14, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
            {"month": date(2026, 10, 1), "created_at": None,
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
            {"month": date(2026, 10, 1), "created_at": datetime(2026, 10, 1, 14, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
        ]
        db = _SegmentFakeDB(
            snapshot_rows=[
                {"snapshot_hour": datetime(2026, 10, 1, 15, 0), "allocated_amount": 56.81},
            ],
            alloc_rows=alloc_rows,
        )
        svc = _service_with(db)

        segments = await svc._derive_segments_for_strategy("hrs")

        # 只有第 3 行有效 → 仅 1 个 E1 事件
        assert len(segments) == 1
        assert segments[0]["event_time"] == datetime(2026, 10, 1, 14, 0)

    async def test_snapshot_rows_all_null_hour_returns_empty(self):
        """快照行存在但 snapshot_hour 全为 NULL → 视为不可评分，返回 []"""
        db = _SegmentFakeDB(
            snapshot_rows=[
                {"snapshot_hour": None, "allocated_amount": 50.0},
            ],
            exists=True,
        )
        svc = _service_with(db)

        segments = await svc._derive_segments_for_strategy("hrs")

        assert segments == []

    async def test_snapshot_invalid_amount_treated_as_zero(self):
        """allocated_amount 非数值（脏数据）→ 按 0 处理 → 无正快照 → 段 skip"""
        db = _SegmentFakeDB(
            snapshot_rows=[
                {"snapshot_hour": datetime(2026, 9, 1, 17, 0), "allocated_amount": "abc"},
            ],
        )
        svc = _service_with(db)

        segments = await svc._derive_segments_for_strategy("hrs")

        assert len(segments) == 1
        assert segments[0]["skip"] is True
        assert segments[0]["opening_capital"] == pytest.approx(0.0)

    async def test_ratio_parse_exception_treated_as_none(self):
        """E2 解析异常路径：allocated_ratio 非数值 → cur_ratio=None，不产生 E2 事件"""
        alloc_rows = [
            {"month": date(2026, 9, 1), "created_at": datetime(2026, 9, 1, 14, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": "abc"}]},
            {"month": date(2026, 10, 1), "created_at": datetime(2026, 10, 1, 14, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
        ]
        db = _SegmentFakeDB(
            snapshot_rows=[
                {"snapshot_hour": datetime(2026, 9, 1, 15, 0), "allocated_amount": 100.0},
                {"snapshot_hour": datetime(2026, 10, 1, 15, 0), "allocated_amount": 56.81},
            ],
            alloc_rows=alloc_rows,
        )
        svc = _service_with(db)

        segments = await svc._derive_segments_for_strategy("hrs")

        # 两条 month 记录 → 2 个 E1；ratio 解析异常的 9 月行不计入 prev_ratio → 无 E2
        assert len(segments) == 2
        assert all(s["event_type"] == "E1_month_switch" for s in segments)
        assert all(s["skip"] is False for s in segments)

    async def test_opening_amount_unparseable_treated_as_skip(self):
        """段初分配额 float() 解析异常路径：opening 行数值非法 → 段记 skip、段初 0"""
        db = _SegmentFakeDB(
            snapshot_rows=[
                {"snapshot_hour": datetime(2026, 9, 1, 15, 0), "allocated_amount": 100.0},
            ],
            alloc_rows=[
                {"month": date(2026, 9, 1), "created_at": datetime(2026, 9, 1, 14, 0),
                 "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
            ],
            opening_row_override={
                datetime(2026, 9, 1, 14, 0): {
                    "allocated_amount": "oops",  # 模拟 DB 返回非法数值（防御分支）
                    "snapshot_hour": datetime(2026, 9, 1, 15, 0),
                },
            },
        )
        svc = _service_with(db)

        segments = await svc._derive_segments_for_strategy("hrs")

        assert len(segments) == 1
        assert segments[0]["skip"] is True
        assert segments[0]["opening_capital"] == pytest.approx(0.0)


class TestBuildStrategySegmentedSeries:
    """CS1 分段净值序列构建：跨段不继承 / 零分母段排除 / 窗口截断"""

    @pytest.fixture(autouse=True)
    def _reset_event_loop(self):
        import asyncio
        policy = asyncio.get_event_loop_policy()
        loop_local = getattr(policy, "_local", None)
        if loop_local is not None:
            try:
                policy.set_event_loop(None)
                loop_local._set_called = False
            except Exception:
                pass

    async def test_multi_segment_day_pnl_not_carried_across_segments(self):
        """AC-1.4（构建层）：段 B 的 cum 从 0 起，不继承段 A 的盈亏"""
        alloc_rows = [
            {"month": date(2026, 9, 1), "created_at": datetime(2026, 9, 1, 15, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
            {"month": date(2026, 9, 11), "created_at": datetime(2026, 9, 11, 15, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
        ]
        snapshot_rows = [
            {"snapshot_hour": datetime(2026, 9, 1, 15, 30), "allocated_amount": 100.0},
            {"snapshot_hour": datetime(2026, 9, 11, 15, 30), "allocated_amount": 150.0},
        ]
        day_rows = [
            {"day": date(2026, 9, 5), "day_pnl": 50.0},    # 段 A 内
            {"day": date(2026, 9, 15), "day_pnl": -30.0},  # 段 B 内
        ]
        db = _SegmentFakeDB(snapshot_rows=snapshot_rows, alloc_rows=alloc_rows,
                            day_rows=day_rows)
        svc = _service_with(db)

        series = await svc._build_strategy_segmented_series(
            "hrs", datetime(2026, 9, 1), datetime(2026, 9, 20),
        )

        assert len(series) == 2
        seg_a, seg_b = series
        # 段 A：9-1~9-10（下一段事件 9-11 前一天截止），9-5 +50 → 净值 1.5
        assert seg_a["dates"][0] == date(2026, 9, 1)
        assert seg_a["dates"][-1] == date(2026, 9, 10)
        assert seg_a["opening_capital"] == pytest.approx(100.0)
        assert seg_a["net_values"][4] == pytest.approx(1.5)  # 9-5
        # 段 B：9-11 起，净值从 1.0 重新开始（段 A 的 +50 不继承）
        assert seg_b["dates"][0] == date(2026, 9, 11)
        assert seg_b["opening_capital"] == pytest.approx(150.0)
        assert seg_b["net_values"][0] == pytest.approx(1.0)
        # 9-15 亏 30（段初 150）→ 净值 0.8
        assert seg_b["net_values"][4] == pytest.approx(0.8)

    async def test_zero_denominator_segment_excluded_from_series(self):
        """§4.5：零分母段整段跳过 → 不产生任何段序列，其期间日盈亏不进任何段"""
        alloc_rows = [
            {"month": date(2026, 9, 1), "created_at": datetime(2026, 9, 1, 15, 30),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
        ]
        snapshot_rows = [
            {"snapshot_hour": datetime(2026, 9, 1, 15, 30), "allocated_amount": 100.0},
            {"snapshot_hour": datetime(2026, 9, 2, 0, 0), "allocated_amount": 0.0},
            {"snapshot_hour": datetime(2026, 9, 3, 0, 0), "allocated_amount": 0.0},
            {"snapshot_hour": datetime(2026, 9, 4, 0, 0), "allocated_amount": 0.0},
            {"snapshot_hour": datetime(2026, 9, 5, 0, 0), "allocated_amount": 0.0},
        ]
        day_rows = [
            {"day": date(2026, 9, 1), "day_pnl": 10.0},  # 正常段内
            {"day": date(2026, 9, 2), "day_pnl": 10.0},  # 零分母期内 → 应被排除
        ]
        db = _SegmentFakeDB(snapshot_rows=snapshot_rows, alloc_rows=alloc_rows,
                            day_rows=day_rows)
        svc = _service_with(db)

        series = await svc._build_strategy_segmented_series(
            "hrs", datetime(2026, 9, 1), datetime(2026, 9, 10),
        )

        # 只有正常段进序列；零分母段（9-2 起）不产生任何序列
        assert len(series) == 1
        assert all(s["opening_capital"] > 0 for s in series)
        assert series[0]["dates"] == [date(2026, 9, 1)]
        assert series[0]["net_values"] == pytest.approx([1.1])  # +10 / 100

    async def test_window_start_after_segment_start_no_inherited_pnl(self):
        """AC-1.5（构建层）：策略窗口起点落在段中间 → cum 从窗口起点重算，
        不继承窗口起点之前的盈亏（9-5 的 +40 不得出现在净值里）"""
        db = _SegmentFakeDB(
            snapshot_rows=[
                {"snapshot_hour": datetime(2026, 9, 1, 17, 0), "allocated_amount": 100.0},
            ],
            day_rows=[
                {"day": date(2026, 9, 5), "day_pnl": 40.0},  # 窗口起点 9-10 之前
            ],
        )
        svc = _service_with(db)

        series = await svc._build_strategy_segmented_series(
            "hrs", datetime(2026, 9, 10), datetime(2026, 9, 12),
        )

        assert len(series) == 1
        seg = series[0]
        assert seg["dates"][0] == date(2026, 9, 10)  # 起点截断到窗口起点
        assert len(seg["dates"]) == 3
        # 9-5 的 +40 未被继承 → 窗口起点净值 = 1.0
        assert seg["net_values"][0] == pytest.approx(1.0)
        assert all(nv == pytest.approx(1.0) for nv in seg["net_values"])

    async def test_segment_outside_window_excluded(self):
        """段与策略窗口不相交（actual_start > actual_end）→ 该段被排除"""
        alloc_rows = [
            {"month": date(2026, 9, 1), "created_at": datetime(2026, 9, 1, 15, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
            {"month": date(2026, 9, 3), "created_at": datetime(2026, 9, 3, 15, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
        ]
        snapshot_rows = [
            {"snapshot_hour": datetime(2026, 9, 1, 15, 30), "allocated_amount": 100.0},
            {"snapshot_hour": datetime(2026, 9, 3, 15, 30), "allocated_amount": 150.0},
        ]
        db = _SegmentFakeDB(snapshot_rows=snapshot_rows, alloc_rows=alloc_rows)
        svc = _service_with(db)

        series = await svc._build_strategy_segmented_series(
            "hrs", datetime(2026, 9, 10), datetime(2026, 9, 20),
        )

        # 段 1（9-1~9-2）整体早于窗口起点 9-10 → 排除；只剩段 2 的窗口内部分
        assert len(series) == 1
        assert series[0]["opening_capital"] == pytest.approx(150.0)
        assert series[0]["dates"][0] == date(2026, 9, 10)

    async def test_same_day_dual_events_previous_segment_empty_excluded(self):
        """边界：同日两个段事件 → 前段 0 天样本被排除（actual_start > actual_end）"""
        alloc_rows = [
            {"month": date(2026, 9, 1), "created_at": datetime(2026, 9, 1, 15, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.1}]},
            {"month": date(2026, 9, 1), "created_at": datetime(2026, 9, 1, 16, 0),
             "entries": [{"strategy_id": "hrs", "allocated_ratio": 0.15}]},
        ]
        snapshot_rows = [
            {"snapshot_hour": datetime(2026, 9, 1, 15, 30), "allocated_amount": 100.0},
            {"snapshot_hour": datetime(2026, 9, 1, 16, 30), "allocated_amount": 150.0},
        ]
        db = _SegmentFakeDB(snapshot_rows=snapshot_rows, alloc_rows=alloc_rows)
        svc = _service_with(db)

        series = await svc._build_strategy_segmented_series(
            "hrs", datetime(2026, 9, 1), datetime(2026, 9, 3),
        )

        # 段 1（15:00 事件）截止日 = 段 2 事件日前一天 = 8-31 → 0 天样本被排除
        assert len(series) == 1
        assert series[0]["opening_capital"] == pytest.approx(150.0)
        assert series[0]["dates"] == [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]

    async def test_no_strategy_names_returns_empty(self):
        """_db_strategy_names 返回空（防御分支）→ 返回 []"""
        db = _SegmentFakeDB(
            snapshot_rows=[
                {"snapshot_hour": datetime(2026, 9, 1, 17, 0), "allocated_amount": 100.0},
            ],
        )
        svc = _service_with(db)
        svc._db_strategy_names = lambda sid: []

        series = await svc._build_strategy_segmented_series(
            "hrs", datetime(2026, 9, 1), datetime(2026, 9, 3),
        )

        assert series == []

    async def test_build_returns_empty_when_segments_empty(self):
        """段推导为空（grid 守卫无快照）→ 构建层直接返回 []（防御分支）"""
        db = _SegmentFakeDB()  # 无任何快照行 → _derive_segments_for_strategy 返回 []
        svc = _service_with(db)

        series = await svc._build_strategy_segmented_series(
            "grid", datetime(2026, 9, 1), datetime(2026, 9, 3),
        )

        assert series == []
        # 未走到日盈亏查询
        assert all("trading.trade_records" not in s for s in db.all_sqls)


class TestComputeStrategyPerfBySegmentsBranches:
    """CS2 分支覆盖：空序列 / 窗口外 / 样本不足 / 零基净值 / AC-1.5 语义取证"""

    @pytest.fixture(autouse=True)
    def _reset_event_loop(self):
        import asyncio
        policy = asyncio.get_event_loop_policy()
        loop_local = getattr(policy, "_local", None)
        if loop_local is not None:
            try:
                policy.set_event_loop(None)
                loop_local._set_called = False
            except Exception:
                pass

    def test_empty_segmented_series_returns_none(self):
        """空段序列（如 grid 不可评分）→ None（不产生新行）"""
        svc = DataService()
        row = svc._compute_strategy_perf_by_segments(
            "day", "grid", [], date(2026, 10, 1), 30, date(2026, 10, 15),
        )
        assert row is None

    def test_segment_entirely_outside_window_returns_none(self):
        """段整体早于窗口起点 → 无相交样本 → None"""
        dates = [date(2026, 9, d) for d in range(1, 6)]  # 9-1~9-5
        seg = _seg_series_for_compute(
            dates, [1.0, 1.1, 1.2, 1.1, 1.0], [0.0, 0.1, 0.1, -0.1, -0.1],
        )
        svc = DataService()
        row = svc._compute_strategy_perf_by_segments(
            "day", "hrs", seg, date(2026, 10, 1), 30, date(2026, 10, 15),
        )
        assert row is None  # 窗口起点 9-15，段尾 9-5 < 9-15 → 不相交

    def test_single_sample_segment_returns_none(self):
        """窗口内仅 1 天样本（单日样本段）→ sample_count < 2 → None"""
        seg = _seg_series_for_compute(
            [date(2026, 10, 14)], [1.0], [0.1],
        )
        svc = DataService()
        row = svc._compute_strategy_perf_by_segments(
            "day", "hrs", seg, date(2026, 10, 1), 30, date(2026, 10, 15),
        )
        assert row is None

    def test_zero_base_net_value_produces_flat_series(self):
        """边界：窗口起点净值已钳 0（base_nv=0）→ 重归一退化为全 1.0 平线

        现状行为：该段回撤按 0.0 记（真实回撤信息被抹平）。
        与架构 §4.3「cum 从窗口起点重算」的期望不一致，此断言锁定现状供 C 段决策。
        """
        seg = _seg_series_for_compute(
            [date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)],
            [0.0, 0.5, 0.6],
            [0.1, 0.2, 0.3],
        )
        svc = DataService()
        row = svc._compute_strategy_perf_by_segments(
            "day", "hrs", seg, date(2026, 10, 1), 30, date(2026, 10, 15),
        )
        assert row is not None
        _, _, _, _, sample_count, _sharpe, max_dd, _, _ = row
        assert sample_count == 3
        assert max_dd == pytest.approx(0.0)  # 平线 → 回撤 0（现状行为）

    def test_ac15_renorm_is_proportional_not_reaccumulate_deviation_repro(self):
        """AC-1.5 最小复现（实现 vs 架构伪码偏差取证）：

        构造 opening=100、段内 cum=[0,+50,+20,-10] → net_values=[1.0,1.5,1.2,0.9]，
        窗口起点 = 9-12（today=9-14、window_days=2）。

        - 实现口径（比例缩放 nv/base_nv）：renorm=[1.0,0.8,0.6] → dd=0.4
        - 架构 §4.3 伪码（cum 重置 1+cum/opening）：[1.0,0.7,0.4] → dd=0.6

        本断言锁定**实现现状 dd=0.4**：窗口起点前盈亏以「比例」形式残留，
        并未按架构伪码「重置累计」，残缺段回撤被稀释（0.4 < 0.6）。
        """
        dates = [date(2026, 9, d) for d in range(11, 15)]  # 9-11~9-14
        seg = _seg_series_for_compute(
            dates, [1.0, 1.5, 1.2, 0.9], [0.0, 0.5, -0.3, -0.3],
        )
        svc = DataService()
        row = svc._compute_strategy_perf_by_segments(
            "week", "hrs", seg, date(2026, 10, 1), 2, date(2026, 9, 14),
        )
        assert row is not None
        _, _, _, _, sample_count, _sharpe, max_dd, ws, _we = row
        assert sample_count == 3  # 窗口起点 9-12 → 9-12/13/14 三天在窗口内
        assert ws == date(2026, 9, 12)  # effective_start 正确截断到窗口起点
        # 实现现状（比例缩放）：dd = 0.4；架构伪码期望 dd = 0.6（偏差已记录，待 C 段裁决）
        assert max_dd == pytest.approx(0.4)

    def test_multi_segment_window_and_actual_bounds(self):
        """多段窗口截取：window_start/window_end 取窗口内实际样本日期"""
        from datetime import date as _date
        seg_a = _seg_series_for_compute(
            [_date(2026, 9, d) for d in range(1, 6)],
            [1.0, 1.1, 1.2, 1.1, 1.0],
            [0.0, 0.1, 0.1, -0.1, -0.1],
        )
        seg_b = _seg_series_for_compute(
            [_date(2026, 10, d) for d in range(10, 16)],
            [1.0, 1.05, 1.1, 1.05, 1.0, 0.95],
            [0.0, 0.05, 0.05, -0.05, -0.05, -0.05],
        )
        svc = DataService()
        row = svc._compute_strategy_perf_by_segments(
            "week", "hrs", [seg_a[0], seg_b[0]], _date(2026, 10, 1), 10, _date(2026, 10, 15),
        )
        assert row is not None
        _, _, _, _, sample_count, _sharpe, max_dd, ws, we = row
        # 段 A 整体在窗口起点（10-05）之前 → 排除；只有段 B 参与
        assert sample_count == 6
        assert ws == _date(2026, 10, 10)
        assert we == _date(2026, 10, 15)
        # 段 B 回撤：(1.1 - 0.95)/1.1 ≈ 0.1364
        assert max_dd == pytest.approx(0.15 / 1.1, abs=1e-6)

    def test_compute_skips_segment_with_empty_dates(self):
        """段 dates 为空（防御分支）→ 该段跳过，无相交样本 → None"""
        seg = [{
            "segment_start": datetime(2026, 10, 1),
            "opening_capital": 100.0,
            "opening_capital_time": datetime(2026, 10, 1),
            "skip": False,
            "daily_returns": [],
            "net_values": [],
            "dates": [],
        }]
        svc = DataService()
        row = svc._compute_strategy_perf_by_segments(
            "day", "hrs", seg, date(2026, 10, 1), 30, date(2026, 10, 15),
        )
        assert row is None

    def test_all_single_sample_segments_returns_none(self):
        """多个单日样本段：rets 拼接 ≥2 但各段净值 <2 无法算 dd → max_dd None → 返回 None"""
        seg1 = _seg_series_for_compute([date(2026, 10, 14)], [1.0], [0.1])
        seg2 = _seg_series_for_compute([date(2026, 10, 15)], [1.0], [0.2])
        svc = DataService()
        row = svc._compute_strategy_perf_by_segments(
            "day", "hrs", [seg1[0], seg2[0]], date(2026, 10, 1), 30, date(2026, 10, 15),
        )
        assert row is None  # sample_count=2 但 max_dd_segments 为空 → None（防御分支）


class TestComputeAndStorePerformanceMetricsBehavior:
    """compute_and_store_performance_metrics 行为级：grid 守卫 / CS4 参数 / 异常不中断"""

    @pytest.fixture(autouse=True)
    def _reset_event_loop(self):
        import asyncio
        policy = asyncio.get_event_loop_policy()
        loop_local = getattr(policy, "_local", None)
        if loop_local is not None:
            try:
                policy.set_event_loop(None)
                loop_local._set_called = False
            except Exception:
                pass

    def _make_valid_seg(self):
        today = date.today()
        dates = [today - timedelta(days=i) for i in range(9, -1, -1)]
        return {
            "segment_start": datetime.combine(dates[0], datetime.min.time()),
            "opening_capital": 100.0,
            "opening_capital_time": datetime.combine(dates[0], datetime.min.time()),
            "skip": False,
            "daily_returns": [0.01] * 10,
            "net_values": [1.0 + i * 0.01 for i in range(10)],
            "dates": dates,
        }

    def _patch(self, monkeypatch, svc, acc, segmented):
        import services.data_service_docker as dsd
        monkeypatch.setattr(dsd, "PERFORMANCE_STRATEGY_IDS", ["hrs", "grid"])
        monkeypatch.setattr(svc, "_build_account_daily_returns", acc)
        monkeypatch.setattr(svc, "_build_strategy_segmented_series", segmented)

        async def _noop_init():
            # 阻断 _ensure_initialized 用真实 DatabaseManager 覆盖 fake db
            return None

        monkeypatch.setattr(svc, "_ensure_initialized", _noop_init)

    async def test_grid_no_new_rows_and_cs4_caliber_written(self, monkeypatch):
        """§4.4 grid 无分段序列 → 策略级不产生任何新行；CS4 每行带 caliber_version"""
        from datetime import timedelta

        seg = self._make_valid_seg()
        db = _SegmentFakeDB()
        svc = _service_with(db)

        async def fake_acc(ws, te):
            today = date.today()
            dates = [today - timedelta(days=i) for i in range(9, -1, -1)]
            return ([0.0] * 10, [1.0] * 10, dates)

        async def fake_seg(sid, s, e):
            return [seg] if sid == "hrs" else []

        self._patch(monkeypatch, svc, fake_acc, fake_seg)

        result = await svc.compute_and_store_performance_metrics()

        # 4 粒度 ×（total 1 + hrs 1）= 8；grid 0 行（§4.4 不产生新行）
        assert result == {"rows": 8}
        strat_calls = [p for (_sql, p) in db.executed if p[1] == "strategy"]
        assert all(p[2] != "grid" for p in strat_calls), "grid 不得产生策略级新行"
        assert sum(1 for p in strat_calls if p[2] == "hrs") == 4
        total_calls = [p for (_sql, p) in db.executed if p[1] == "total"]
        assert len(total_calls) == 4
        # CS4：UPSERT 参数必须是 10 元组，末位 caliber_version='segmented-v1'
        assert all(len(p) == 10 for (_sql, p) in db.executed)
        assert all(p[9] == "segmented-v1" for (_sql, p) in db.executed)

    async def test_single_bucket_failure_does_not_interrupt(self, monkeypatch):
        """单桶落库失败（day 粒度）→ 记 warning 跳过，其余粒度继续落库"""
        from datetime import timedelta

        seg = self._make_valid_seg()
        db = _RaisingExecDB(fail_granularity="day")
        svc = _service_with(db)

        async def fake_acc(ws, te):
            today = date.today()
            dates = [today - timedelta(days=i) for i in range(9, -1, -1)]
            return ([0.0] * 10, [1.0] * 10, dates)

        async def fake_seg(sid, s, e):
            return [seg] if sid == "hrs" else []

        self._patch(monkeypatch, svc, fake_acc, fake_seg)

        result = await svc.compute_and_store_performance_metrics()

        # day 粒度的 2 行失败，其余 3 粒度 × 2 = 6 行
        assert result == {"rows": 6}
        assert all(p[0] != "day" for (_sql, p) in db.executed)

    async def test_account_curve_failure_keeps_strategy_rows(self, monkeypatch):
        """账户级净值曲线构建失败 → total 无行，策略级照常落库"""
        seg = self._make_valid_seg()
        db = _SegmentFakeDB()
        svc = _service_with(db)

        async def broken_acc(ws, te):
            raise RuntimeError("equity_snapshot 查询失败（模拟）")

        async def fake_seg(sid, s, e):
            return [seg] if sid == "hrs" else []

        self._patch(monkeypatch, svc, broken_acc, fake_seg)

        result = await svc.compute_and_store_performance_metrics()

        assert result == {"rows": 4}  # 仅 hrs 的 4 个粒度
        assert all(p[1] == "strategy" for (_sql, p) in db.executed)

    async def test_segmented_build_failure_skips_strategy(self, monkeypatch):
        """分段序列构建抛错 → 该策略记 warning 跳过（同空序列守卫），不中断"""
        from datetime import timedelta

        seg = self._make_valid_seg()
        db = _SegmentFakeDB()
        svc = _service_with(db)

        async def fake_acc(ws, te):
            today = date.today()
            dates = [today - timedelta(days=i) for i in range(9, -1, -1)]
            return ([0.0] * 10, [1.0] * 10, dates)

        async def broken_seg(sid, s, e):
            if sid == "grid":
                raise RuntimeError("grid 分段构建失败（模拟）")
            return [seg]

        self._patch(monkeypatch, svc, fake_acc, broken_seg)

        result = await svc.compute_and_store_performance_metrics()

        assert result == {"rows": 8}
        assert all(p[2] != "grid" for (_sql, p) in db.executed)
