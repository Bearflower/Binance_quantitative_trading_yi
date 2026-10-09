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
from datetime import date, datetime

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
            return list(self.trade_rows)
        return []

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


class TestStrategyCurveBaseCapital:
    """策略净值曲线：固定基数 + 累计盈亏（不复利）"""

    async def test_net_value_never_negative_and_dd_capped_at_100pct(self):
        """亏损量级超过资金基数时：净值钳到 0，最大回撤上限 100%"""
        db = _FakeDB(
            trade_rows=[{"day": date(2026, 9, 3), "day_pnl": -150.0}],
            snapshots=[
                {"snapshot_hour": datetime(2026, 9, 1, 0, 0),
                 "allocated_amount": 100.0, "open_margin": 80.0},
            ],
        )
        svc = _service_with(db)
        rets, net_vals, dates = await svc._build_strategy_daily_returns(
            "hrs", datetime(2026, 9, 1), datetime(2026, 9, 4),
        )

        assert len(rets) == len(net_vals) == len(dates) == 4
        # 净值一律非负（本金亏光 = 0，不允许为负）
        assert all(v >= 0 for v in net_vals)
        # 09-03 亏 150（=1.5 倍基数）→ 净值 0.0，回撤 = (1.0 - 0.0) / 1.0 = 1.0
        assert net_vals[2] == pytest.approx(0.0)
        assert max_drawdown_ratio(net_vals) == pytest.approx(1.0)
        assert max_drawdown_ratio(net_vals) <= 1.0

    async def test_no_compounding(self):
        """连续两日各亏 50%（基数 100）：不复利 → [0.5, 0.0]，而非复利的 [0.5, 0.25]"""
        db = _FakeDB(
            trade_rows=[
                {"day": date(2026, 9, 1), "day_pnl": -50.0},
                {"day": date(2026, 9, 2), "day_pnl": -50.0},
            ],
            snapshots=[
                {"snapshot_hour": datetime(2026, 9, 1, 0, 0),
                 "allocated_amount": 100.0, "open_margin": 100.0},
            ],
        )
        svc = _service_with(db)
        rets, net_vals, _ = await svc._build_strategy_daily_returns(
            "hrs", datetime(2026, 9, 1), datetime(2026, 9, 2),
        )

        assert rets == pytest.approx([-0.5, -0.5])
        assert net_vals == pytest.approx([0.5, 0.0])

    async def test_profit_curve_is_base_plus_cum_pnl(self):
        """盈利时曲线 = 基数 + 累计盈亏（线性，不复利）"""
        db = _FakeDB(
            trade_rows=[
                {"day": date(2026, 9, 1), "day_pnl": 10.0},
                {"day": date(2026, 9, 2), "day_pnl": 10.0},
            ],
            snapshots=[
                {"snapshot_hour": datetime(2026, 9, 1, 0, 0),
                 "allocated_amount": 100.0, "open_margin": 100.0},
            ],
        )
        svc = _service_with(db)
        rets, net_vals, _ = await svc._build_strategy_daily_returns(
            "hrs", datetime(2026, 9, 1), datetime(2026, 9, 2),
        )

        assert net_vals == pytest.approx([1.10, 1.20])
        assert rets == pytest.approx([0.10, 0.10])


class TestBaseCapitalQueryNoFutureSnapshot:
    """资金基数查询：带时间上界 / 不再按月翻找未来快照"""

    async def test_all_queries_have_upper_bound_or_are_earliest_fallback(self):
        """所有快照查询不得再出现「无上界的 >= 正向排序」（旧 bug 写法）"""
        db = _FakeDB(
            trade_rows=[],
            snapshots=[
                {"snapshot_hour": datetime(2026, 9, 10, 22, 0),
                 "allocated_amount": 76.82, "open_margin": 76.82},
            ],
        )
        svc = _service_with(db)
        await svc._build_strategy_daily_returns(
            "hrs", datetime(2026, 7, 1), datetime(2026, 7, 3),
        )

        assert db.one_sqls, "应至少查询一次资金基数"
        for sql in db.one_sqls:
            # 旧 bug：`snapshot_hour >= $2 ... ORDER BY ASC`（无月份上界 → 落到未来快照）
            assert "snapshot_hour >=" not in sql

    async def test_base_capital_query_count_independent_of_window_length(self):
        """资金基数全窗口只查一次，查询次数与窗口长度无关（旧实现按月查 → 窗口越长查越多）"""
        snapshot = {"snapshot_hour": datetime(2026, 9, 10, 22, 0),
                    "allocated_amount": 76.82, "open_margin": 76.82}

        db_short = _FakeDB(snapshots=[snapshot])
        await _service_with(db_short)._build_strategy_daily_returns(
            "hrs", datetime(2026, 7, 1), datetime(2026, 7, 2),
        )
        db_long = _FakeDB(snapshots=[snapshot])
        await _service_with(db_long)._build_strategy_daily_returns(
            "hrs", datetime(2026, 7, 1), datetime(2026, 9, 30),
        )

        # 两窗口覆盖的月份数不同（1 个月 vs 3 个月），但基数查询次数一致且极少
        assert len(db_short.one_sqls) == len(db_long.one_sqls)
        assert len(db_long.one_sqls) <= 2

    async def test_fallback_to_open_margin_when_no_allocation(self):
        """无 allocated_amount 时退化为占用保证金（含杠杆）作为兜底基数"""
        db = _FakeDB(
            trade_rows=[],
            snapshots=[
                {"snapshot_hour": datetime(2026, 9, 1, 0, 0),
                 "allocated_amount": 0.0, "open_margin": 60.0},
            ],
        )
        svc = _service_with(db)
        base = await svc._query_strategy_base_capital("hrs", datetime(2026, 9, 1))
        assert base == pytest.approx(60.0)

    async def test_returns_zero_when_no_snapshot(self):
        """完全无快照 → 0.0，策略绩效跳过"""
        svc = _service_with(_FakeDB())
        assert await svc._query_strategy_base_capital("hrs", datetime(2026, 9, 1)) == 0.0
        rets, net_vals, dates = await svc._build_strategy_daily_returns(
            "hrs", datetime(2026, 9, 1), datetime(2026, 9, 3),
        )
        assert (rets, net_vals, dates) == ([], [], [])


class TestPerfEntryWindow:
    """_compute_one_perf_entry：窗口截取与实际样本窗口落库字段"""

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

