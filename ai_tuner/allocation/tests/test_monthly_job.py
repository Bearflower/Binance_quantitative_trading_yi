"""
月度资金分配主流程（MonthlyAllocationJob）单元测试（需求 §14.1/§14.3）

测试目标：
  - _next_month / _resolve_months 纯函数（含 12 月跨年）
  - _resolve_month_inputs 月份入参解析
  - _calculate_month_range 盈亏归属月采集窗口（左闭右开、跨年、边界、非法格式）
  - run_monthly_allocation 集成（mock DB）：
      AC-1.1 生效月写库 + 盈亏归属月窗口
      AC-1.2 幂等命中不写库不通知
      AC-1.3 跨年（12/31 → 2027-01）
      AC-1.4 五处月份一致（AllocationResult/写库/配置/通知/返回值）
      AC-1.5 消费方按当前月查询的契约
      F4    幂等检查异常 → ON CONFLICT 双保险不重复写入
      AC-3.1~3.4 显式月份补生成 2026-10
  - scripts/manual_allocation_trigger.py 的 argparse 参数支持
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from pathlib import Path

# 确保项目根目录在 sys.path 中，以便导入 ai_tuner / shared 模块
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ai_tuner.allocation.allocation_calculator import (  # noqa: E402
    AllocationEntry,
    AllocationResult,
)
from ai_tuner.allocation.config_updater import AllocationConfigUpdater  # noqa: E402
from ai_tuner.allocation.daily_refresher import DailyAllocationRefresher  # noqa: E402
from ai_tuner.allocation.monthly_job import (  # noqa: E402
    CST,
    MonthlyAllocationJob,
    _next_month,
    _resolve_months,
)

# 中国标准时间
_CST = timezone(timedelta(hours=8))

# 与 ai_tuner/config.yaml 一致的排名档（总和 85%）
_RANK_RATIOS = [0.30, 0.25, 0.20, 0.10]
_STRATEGY_IDS = ["btc_eth", "btc_eth_aggressive", "new_coin", "hrs"]
_STRATEGY_NAMES = {
    "btc_eth": "MTPCS策略",
    "btc_eth_aggressive": "MTPCS激进策略",
    "new_coin": "新币做空策略",
    "hrs": "HRS混合反转策略",
}


class _FrozenDateTime(datetime):
    """可冻结 now 的 datetime 替身（strptime 等继承原生行为）"""

    frozen = None

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.frozen.replace(tzinfo=None)
        return cls.frozen.astimezone(tz)


class _FakeMessenger:
    """伪造消息发送器：记录通知卡片与错误通知"""

    def __init__(self, card_raises=False):
        self.cards = []
        self.errors = []
        self.card_raises = card_raises

    async def send_allocation_card(self, result):
        self.cards.append(result)
        if self.card_raises:
            raise RuntimeError("通知发送失败")

    async def send_error_notification(self, **kwargs):
        self.errors.append(kwargs)


class _FakeDb:
    """按 SQL 文本路由的伪造数据库，记录调用并模拟 capital_allocation 幂等写入"""

    def __init__(
        self,
        *,
        pnl=None,
        capital=None,
        trade_count=None,
        existing_months=None,
        idem_raises=False,
        ddl_raises=False,
        first_month_raises=False,
        first_month_cnt=2,
    ):
        self.pnl = pnl or {}
        self.capital = capital or {}
        self.trade_count = trade_count or {}
        self.existing_months = set(existing_months or [])
        self.idem_raises = idem_raises
        self.ddl_raises = ddl_raises
        self.first_month_raises = first_month_raises
        self.first_month_cnt = first_month_cnt
        self.ddl_calls = []
        self.fetch_calls = []      # (kind, args)
        self.executes = []         # (query, args)
        self.inserts = []          # capital_allocation 插入参数

    async def execute_ddl(self, query):
        self.ddl_calls.append(query)
        if self.ddl_raises:
            raise RuntimeError("DDL 失败（模拟）")

    async def fetch_one(self, query, *args):
        norm = " ".join(query.split()).upper()
        # 幂等检查（必须最先判断：同样查 capital_allocation 表）
        if "SELECT MONTH, STATUS" in norm:
            self.fetch_calls.append(("idempotency", args))
            if self.idem_raises:
                raise RuntimeError("幂等查询失败（模拟）")
            month = args[0]
            if month in self.existing_months:
                return {"month": month, "status": "active"}
            return None
        # 首月判定（全表 COUNT）
        if "FROM PUBLIC.CAPITAL_ALLOCATION" in norm:
            self.fetch_calls.append(("first_month", args))
            if self.first_month_raises:
                raise RuntimeError("首月查询失败（模拟）")
            return {"cnt": self.first_month_cnt}
        # 零开仓成交行数
        if "COUNT(*) AS CNT" in norm and "TRADING.TRADE_RECORDS" in norm:
            self.fetch_calls.append(("trade_count", args))
            sid = args[0][0]
            return {"cnt": self.trade_count.get(sid, 0)}
        # 当月已实现盈亏
        if "SUM(REALIZED_PNL)" in norm:
            self.fetch_calls.append(("pnl", args))
            sid = args[0][0]
            return {"total_pnl": self.pnl.get(sid, 0.0)}
        # 月初持仓保证金
        if "OPEN_MARGIN" in norm:
            self.fetch_calls.append(("margin", args))
            sid = args[0]
            return {"open_margin": self.capital.get(sid, 0.0)}
        raise AssertionError(f"未预期的查询: {norm}")

    async def execute(self, query, *args):
        self.executes.append((query, args))
        norm = " ".join(query.split()).upper()
        if "INSERT INTO PUBLIC.CAPITAL_ALLOCATION" in norm:
            month = args[0]
            if month in self.existing_months:
                return  # 模拟 ON CONFLICT (month) DO NOTHING
            self.existing_months.add(month)
            self.inserts.append(args)


def _make_config():
    """构造与生产 ai_tuner/config.yaml 结构一致的资金分配配置"""
    return {
        "capital_allocation": {
            "enabled": True,
            "total_capital": 1000.0,
            "reserve_ratio": 0.15,
            "rank_ratios": _RANK_RATIOS,
            "participating_strategies": _STRATEGY_IDS,
            "fallback": {
                "ratios": {sid: 0.25 for sid in _STRATEGY_IDS},
                "capitals": {},
            },
        },
        "strategies": [
            {"strategy_id": sid, "name": _STRATEGY_NAMES[sid]} for sid in _STRATEGY_IDS
        ],
    }


def _default_market_data(*, zero_trade_strategies=()):
    """构造默认行情：4 策略均有正收益率；可指定零开仓策略集合"""
    pnl = {"btc_eth": 300.0, "btc_eth_aggressive": 250.0, "new_coin": 100.0, "hrs": 50.0}
    capital = {sid: 500.0 for sid in _STRATEGY_IDS}
    trade_count = {
        sid: (0 if sid in zero_trade_strategies else 1) for sid in _STRATEGY_IDS
    }
    return pnl, capital, trade_count


def _build_job(db, *, config=None, update_fails=False):
    """构造挂接伪造 DB/通知、复用真实 _save_to_db 的 MonthlyAllocationJob"""
    job = MonthlyAllocationJob(
        config=config or _make_config(),
        db_manager=db,
        notification_client=None,
        messenger=_FakeMessenger(),
        config_operator=None,
        rollback_manager=None,
        binance_client=None,
    )
    real_updater = AllocationConfigUpdater()
    saved_results = []

    async def _fake_update_all(result, config, db_manager, config_operator, rollback_manager):
        # 记录结果用于断言；DB 写入走真实 _save_to_db（含 ON CONFLICT SQL），
        # yaml 文件更新不在本测试范围（由配置更新器自身测试覆盖）
        saved_results.append(result)
        if update_fails:
            return False
        return await real_updater._save_to_db(result, db_manager)

    job.config_updater = SimpleNamespace(update_all=_fake_update_all)
    return job, saved_results


def _minimal_job():
    """仅用于纯函数方法测试的最小实例（无 DB 依赖）"""
    return MonthlyAllocationJob(
        config={"capital_allocation": {}, "strategies": []},
        db_manager=None,
        notification_client=None,
        messenger=None,
        config_operator=None,
        rollback_manager=None,
    )


def _run(job, **kwargs):
    return asyncio.run(job.run_monthly_allocation(**kwargs))


def _freeze(value):
    return patch("ai_tuner.allocation.monthly_job.datetime", _FrozenDateTime)


class TestMonthPureFunctions(unittest.TestCase):
    """_next_month / _resolve_months / _calculate_month_range 纯函数"""

    def test_next_month_normal(self):
        """普通月份递增：1→2、11→12"""
        self.assertEqual(_next_month(2026, 1), (2026, 2))
        self.assertEqual(_next_month(2026, 11), (2026, 12))

    def test_next_month_december_cross_year(self):
        """AC-1.3：12 月跨年到次年 1 月"""
        self.assertEqual(_next_month(2026, 12), (2027, 1))

    def test_resolve_months_september(self):
        """AC-1.1：2026-09-30 09:30 运行 → ('2026-09', '2026-10')"""
        now = datetime(2026, 9, 30, 9, 30, tzinfo=_CST)
        self.assertEqual(_resolve_months(now), ("2026-09", "2026-10"))

    def test_resolve_months_cross_year(self):
        """AC-1.3：2026-12-31 → ('2026-12', '2027-01')"""
        now = datetime(2026, 12, 31, 9, 30, tzinfo=_CST)
        self.assertEqual(_resolve_months(now), ("2026-12", "2027-01"))

    def test_month_range_september(self):
        """AC-1.1：pnl_month=2026-09 → [09-01, 10-01) 左闭右开"""
        job = _minimal_job()
        start, end = job._calculate_month_range("2026-09")
        self.assertEqual(start, datetime(2026, 9, 1, tzinfo=CST))
        self.assertEqual(end, datetime(2026, 10, 1, tzinfo=CST))

    def test_month_range_cross_year(self):
        """AC-1.3：pnl_month=2026-12 → [12-01, 次年 01-01)"""
        job = _minimal_job()
        start, end = job._calculate_month_range("2026-12")
        self.assertEqual(start, datetime(2026, 12, 1, tzinfo=CST))
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=CST))

    def test_month_range_boundaries(self):
        """边界：2 月（28 天）、30 天月、31 天月窗口均以月初/次月 1 日为界"""
        job = _minimal_job()
        cases = {
            "2026-02": (datetime(2026, 2, 1, tzinfo=CST), datetime(2026, 3, 1, tzinfo=CST)),
            "2026-04": (datetime(2026, 4, 1, tzinfo=CST), datetime(2026, 5, 1, tzinfo=CST)),
            "2026-01": (datetime(2026, 1, 1, tzinfo=CST), datetime(2026, 2, 1, tzinfo=CST)),
        }
        for month, expected in cases.items():
            self.assertEqual(job._calculate_month_range(month), expected, month)

    def test_month_range_invalid_format_raises(self):
        """非法月份格式抛 ValueError（避免错月静默写库）"""
        job = _minimal_job()
        with self.assertRaises(ValueError):
            job._calculate_month_range("2026/09")


class TestResolveMonthInputs(unittest.TestCase):
    """_resolve_month_inputs 入参解析三分支"""

    def setUp(self):
        self.job = _minimal_job()
        self.now = datetime(2026, 9, 30, 9, 30, tzinfo=_CST)

    def test_both_none_auto_resolve(self):
        """两者缺省：按运行时刻推导（当月/次月）"""
        self.assertEqual(
            self.job._resolve_month_inputs(self.now, None, None),
            ("2026-09", "2026-10"),
        )

    def test_both_provided_passthrough(self):
        """两者同时提供：原样使用（手动补生成场景）"""
        self.assertEqual(
            self.job._resolve_month_inputs(self.now, "2026-09", "2026-10"),
            ("2026-09", "2026-10"),
        )

    def test_only_pnl_month_raises(self):
        """仅提供盈亏归属月：拒绝，防止月份错配"""
        with self.assertRaises(ValueError):
            self.job._resolve_month_inputs(self.now, "2026-09", None)

    def test_only_effective_month_raises(self):
        """仅提供生效月：拒绝，防止月份错配"""
        with self.assertRaises(ValueError):
            self.job._resolve_month_inputs(self.now, None, "2026-10")


class TestRunMonthlyAllocation(unittest.TestCase):
    """run_monthly_allocation 集成（mock DB）"""

    def _frozen_run(self, job, frozen, **kwargs):
        _FrozenDateTime.frozen = frozen
        with _freeze(frozen):
            return _run(job, **kwargs)

    def _find_calls(self, db, kind):
        return [args for k, args in db.fetch_calls if k == kind]

    def test_ac11_writes_effective_month_with_pnl_window(self):
        """AC-1.1：9/30 运行写 2026-10，PnL 采集窗口为 [09-01, 10-01)"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(pnl=pnl, capital=capital, trade_count=trade_count)
        job, saved = _build_job(db)

        result = self._frozen_run(job, datetime(2026, 9, 30, 9, 30, tzinfo=_CST))

        # 返回字典月份 = 生效月
        self.assertIsNotNone(result)
        self.assertEqual(result["month"], "2026-10")

        # 幂等键按生效月查询
        self.assertEqual(self._find_calls(db, "idempotency"), [("2026-10",)])

        # PnL/成交行数窗口 = 盈亏归属月 9 月（naive，库 TIMESTAMP 无时区）
        pnl_call_args = self._find_calls(db, "pnl")[0]
        self.assertEqual(pnl_call_args[0], ["btc_eth"])
        self.assertEqual(pnl_call_args[1], datetime(2026, 9, 1))
        self.assertEqual(pnl_call_args[2], datetime(2026, 10, 1))
        count_call_args = self._find_calls(db, "trade_count")[0]
        self.assertEqual(count_call_args[1], datetime(2026, 9, 1))
        self.assertEqual(count_call_args[2], datetime(2026, 10, 1))

        # AllocationResult.month 与通知卡片月份一致
        self.assertEqual(saved[0].month, "2026-10")
        self.assertEqual(job.messenger.cards[0].month, "2026-10")

        # 写库 month=2026-10、status=active、4 策略
        self.assertEqual(len(db.inserts), 1)
        insert_args = db.inserts[0]
        self.assertEqual(insert_args[0], "2026-10")
        self.assertEqual(insert_args[2], 4)
        self.assertFalse(insert_args[3])
        entries = json.loads(insert_args[4])
        self.assertEqual(len(entries), 4)
        self.assertEqual(insert_args[5], "active")
        self.assertIn("ON CONFLICT (month) DO NOTHING", db.executes[0][0])

    def test_ac12_idempotent_hit_skips(self):
        """AC-1.2：生效月已存在记录 → 不写库、不通知"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(
            pnl=pnl, capital=capital, trade_count=trade_count,
            existing_months={"2026-10"},
        )
        job, _ = _build_job(db)

        result = self._frozen_run(job, datetime(2026, 9, 30, 9, 30, tzinfo=_CST))

        self.assertIsNone(result)
        self.assertEqual(db.inserts, [])
        self.assertEqual(db.executes, [])
        self.assertEqual(job.messenger.cards, [])

    def test_ac13_cross_year_run(self):
        """AC-1.3：2026-12-31 运行 → 幂等键/写库 2027-01，PnL 窗口 12 月"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(pnl=pnl, capital=capital, trade_count=trade_count)
        job, saved = _build_job(db)

        result = self._frozen_run(job, datetime(2026, 12, 31, 9, 30, tzinfo=_CST))

        self.assertEqual(result["month"], "2027-01")
        self.assertEqual(self._find_calls(db, "idempotency"), [("2027-01",)])
        self.assertEqual(db.inserts[0][0], "2027-01")
        self.assertEqual(saved[0].month, "2027-01")
        pnl_call_args = self._find_calls(db, "pnl")[0]
        self.assertEqual(pnl_call_args[1], datetime(2026, 12, 1))
        self.assertEqual(pnl_call_args[2], datetime(2027, 1, 1))

    def test_f4_idempotency_check_exception_relies_on_conflict(self):
        """F4：幂等检查异常时继续执行，仅一条 INSERT 且含 ON CONFLICT 兜底"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(
            pnl=pnl, capital=capital, trade_count=trade_count,
            idem_raises=True,
        )
        job, _ = _build_job(db)

        result = self._frozen_run(job, datetime(2026, 9, 30, 9, 30, tzinfo=_CST))

        self.assertIsNotNone(result)
        self.assertEqual(len(db.inserts), 1)
        insert_queries = [q for q, _ in db.executes]
        self.assertEqual(len(insert_queries), 1)
        self.assertIn("ON CONFLICT (month) DO NOTHING", insert_queries[0])

    def test_ac31_ac33_explicit_months_backfill(self):
        """AC-3.1/3.3：显式 pnl=2026-09/effective=2026-10，非首月，按排名+零开仓计算"""
        # hrs 零开仓但收益率名义最高：规则必须将其压到第 4 名
        pnl, capital, trade_count = _default_market_data(zero_trade_strategies={"hrs"})
        pnl["hrs"] = 400.0
        db = _FakeDb(
            pnl=pnl, capital=capital, trade_count=trade_count,
            first_month_cnt=2,  # 库中已有 2026-08/2026-09 两条历史
        )
        job, saved = _build_job(db)

        result = _run(job, pnl_month="2026-09", effective_month="2026-10")

        self.assertEqual(result["month"], "2026-10")
        self.assertFalse(result["is_first_month"])
        # 采集窗口仍为 2026-09
        pnl_call_args = self._find_calls(db, "pnl")[0]
        self.assertEqual(pnl_call_args[1], datetime(2026, 9, 1))
        self.assertEqual(pnl_call_args[2], datetime(2026, 10, 1))

        entries = json.loads(db.inserts[0][4])
        self.assertEqual(db.inserts[0][0], "2026-10")
        self.assertEqual(db.inserts[0][5], "active")
        self.assertEqual(len(entries), 4)
        hrs_entry = [e for e in entries if e["strategy_id"] == "hrs"][0]
        self.assertEqual(hrs_entry["rank"], 4)
        self.assertEqual(hrs_entry["allocated_ratio"], 0.10)
        self.assertEqual(saved[0].month, "2026-10")

    def test_ac32_history_records_untouched(self):
        """AC-3.2：补生成只 INSERT 2026-10，无任何 UPDATE/DELETE，不触碰 2026-08/09"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(
            pnl=pnl, capital=capital, trade_count=trade_count,
            existing_months={"2026-08", "2026-09"},
        )
        job, _ = _build_job(db)

        _run(job, pnl_month="2026-09", effective_month="2026-10")

        # 所有写操作均为 capital_allocation 的 INSERT
        self.assertTrue(db.executes)
        for query, args in db.executes:
            norm = " ".join(query.split()).upper()
            self.assertIn("INSERT", norm)
            self.assertNotIn("UPDATE", norm)
            self.assertNotIn("DELETE", norm)
            self.assertEqual(args[0], "2026-10")
        # 幂等检查只针对生效月 2026-10
        idem_months = [args[0] for args in self._find_calls(db, "idempotency")]
        self.assertEqual(idem_months, ["2026-10"])

    def test_ac34_duplicate_backfill_idempotent(self):
        """AC-3.4：同一 effective_month 连续执行两次，仅一条记录"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(pnl=pnl, capital=capital, trade_count=trade_count)
        job, _ = _build_job(db)

        first = _run(job, pnl_month="2026-09", effective_month="2026-10")
        second = _run(job, pnl_month="2026-09", effective_month="2026-10")

        self.assertIsNotNone(first)
        self.assertIsNone(second)  # 第二次幂等命中
        self.assertEqual(len(db.inserts), 1)
        self.assertEqual(db.inserts[0][0], "2026-10")
        self.assertEqual(len(job.messenger.cards), 1)

    def test_disabled_skips_without_query(self):
        """未启用时直接跳过，不触发任何业务查询/写入"""
        config = _make_config()
        config["capital_allocation"]["enabled"] = False
        db = _FakeDb()
        job, _ = _build_job(db, config=config)

        result = _run(job)

        self.assertIsNone(result)
        self.assertEqual(db.fetch_calls, [])
        self.assertEqual(db.executes, [])

    def test_empty_pnl_data_skips(self):
        """无参与策略（采集为空）时跳过，不写库"""
        config = {
            "capital_allocation": {
                "enabled": True,
                "total_capital": 1000.0,
                "reserve_ratio": 0.15,
                "rank_ratios": _RANK_RATIOS,
                "participating_strategies": [],
                "fallback": {"ratios": {}, "capitals": {}},
            },
            "strategies": [],
        }
        db = _FakeDb()
        job, _ = _build_job(db, config=config)

        result = _run(job)

        self.assertIsNone(result)
        self.assertEqual(db.inserts, [])

    def test_update_failure_sends_error_notification(self):
        """存储更新失败时发错误通知并返回 None"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(pnl=pnl, capital=capital, trade_count=trade_count)
        job, _ = _build_job(db, update_fails=True)

        result = _run(job)

        self.assertIsNone(result)
        self.assertEqual(len(job.messenger.errors), 1)

    def test_outer_exception_returns_none(self):
        """主流程异常（如缺少 total_capital 配置）被捕获并返回 None"""
        config = _make_config()
        del config["capital_allocation"]["total_capital"]
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(pnl=pnl, capital=capital, trade_count=trade_count)
        job, _ = _build_job(db, config=config)

        self.assertIsNone(_run(job))

    def test_partial_month_arguments_rejected(self):
        """只传一个月份参数时运行期拒绝（ValueError 被外层捕获，返回 None）"""
        db = _FakeDb()
        job, _ = _build_job(db)

        self.assertIsNone(_run(job, pnl_month="2026-09"))
        self.assertEqual(db.inserts, [])

    def test_ddl_failure_does_not_block(self):
        """建表异常仅告警，不阻断后续流程（既有容错路径）"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(
            pnl=pnl, capital=capital, trade_count=trade_count,
            ddl_raises=True,
        )
        job, _ = _build_job(db)

        result = _run(job)

        self.assertIsNotNone(result)
        self.assertEqual(len(db.inserts), 1)

    def test_first_month_query_exception_defaults_non_first(self):
        """F6/F7：首月判定异常默认非首月（接受现状，记录的失败状态）"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(
            pnl=pnl, capital=capital, trade_count=trade_count,
            first_month_raises=True,
        )
        job, saved = _build_job(db)

        result = _run(job)

        self.assertIsNotNone(result)
        self.assertFalse(result["is_first_month"])
        self.assertFalse(saved[0].is_first_month)

    def test_non_participating_strategy_excluded(self):
        """非参与策略即使存在于 strategies 配置中也不参与采集与分配"""
        config = _make_config()
        config["strategies"].append({"strategy_id": "grid", "name": "网格策略"})
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(pnl=pnl, capital=capital, trade_count=trade_count)
        job, _ = _build_job(db, config=config)

        # 构造期即完成筛选
        self.assertEqual(
            [s["strategy_id"] for s in job._strategy_configs], _STRATEGY_IDS
        )

        result = _run(job)

        self.assertEqual(len(result["entries"]), 4)
        pnl_sids = {args[0][0] for args in self._find_calls(db, "pnl")}
        self.assertEqual(pnl_sids, set(_STRATEGY_IDS))
        self.assertNotIn("grid", pnl_sids)

    def test_uses_exchange_balance_when_available(self):
        """交易所净资产查询成功且为正时，总资金取交易所值而非配置值"""
        from decimal import Decimal

        class _FakeBinance:
            async def get_account_info(self):
                return {"totalMarginBalance": Decimal("1200")}

        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(pnl=pnl, capital=capital, trade_count=trade_count)
        job, _ = _build_job(db)
        job.binance_client = _FakeBinance()

        result = _run(job)

        self.assertIsNotNone(result)
        self.assertEqual(result["total_capital"], 1200.0)
        self.assertEqual(db.inserts[0][1], 1200.0)

    def test_first_month_cnt_zero_uses_fallback(self):
        """首月判定 COUNT=0 → is_first_month=True，走 fallback 比例"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(
            pnl=pnl, capital=capital, trade_count=trade_count,
            first_month_cnt=0,
        )
        job, saved = _build_job(db)

        result = _run(job)

        self.assertIsNotNone(result)
        self.assertTrue(result["is_first_month"])
        self.assertTrue(saved[0].is_first_month)
        # fallback 每策略 0.25（见 _make_config）
        self.assertEqual(
            [e["allocated_ratio"] for e in result["entries"]], [0.25] * 4
        )

    def test_notification_exception_swallowed(self):
        """通知发送异常不影响分配结果返回（既有容错路径）"""
        pnl, capital, trade_count = _default_market_data()
        db = _FakeDb(pnl=pnl, capital=capital, trade_count=trade_count)
        job = MonthlyAllocationJob(
            config=_make_config(),
            db_manager=db,
            notification_client=None,
            messenger=_FakeMessenger(card_raises=True),
            config_operator=None,
            rollback_manager=None,
            binance_client=None,
        )
        saved = []
        real_updater = AllocationConfigUpdater()

        async def _update_all(result, config, db_manager, config_operator, rollback_manager):
            saved.append(result)
            return await real_updater._save_to_db(result, db_manager)

        job.config_updater = SimpleNamespace(update_all=_update_all)

        result = _run(job)

        self.assertIsNotNone(result)
        self.assertEqual(result["month"], db.inserts[0][0])


class TestAllocationMonthPropagation(unittest.TestCase):
    """AC-1.4：AllocationResult.month 向各策略 config allocation_month 的传播"""

    def _make_result(self, month="2026-10"):
        return AllocationResult(
            month=month,
            total_capital=1000.0,
            reserve_amount=150.0,
            allocatable_amount=850.0,
            is_first_month=False,
            entries=[
                AllocationEntry(
                    strategy_id="btc_eth",
                    strategy_name="MTPCS策略",
                    realized_pnl=300.0,
                    initial_capital=500.0,
                    return_rate=0.6,
                    rank=1,
                    allocated_ratio=0.30,
                    allocated_amount=300.0,
                )
            ],
        )

    def test_tuner_config_allocation_month(self):
        """ai_tuner/config.yaml 的 capital_limits.allocation_month = 生效月"""

        class _FakeOperator:
            def __init__(self):
                self.calls = []

            def apply_changes(self, config_path, adjustments):
                self.calls.append((config_path, adjustments))
                return True

        operator = _FakeOperator()
        updater = AllocationConfigUpdater()
        ok = asyncio.run(
            updater._update_tuner_config(self._make_result(), {}, operator)
        )

        self.assertTrue(ok)
        capital_limits = operator.calls[0][1]["capital_limits"]
        self.assertEqual(capital_limits["allocation_month"], "2026-10")
        self.assertIn("risk_reserve", capital_limits)
        self.assertIn("total", capital_limits)

    def test_strategy_config_allocation_month(self):
        """各策略 config.yaml 的 allocation_month 与 account_ratio_cap = 生效月比例"""

        class _FakeOperator:
            def __init__(self):
                self.calls = []

            def apply_changes(self, config_path, adjustments):
                self.calls.append((config_path, adjustments))
                return True

        # 策略配置路径必须真实存在（_update_strategy_configs 会 os.path.exists）
        tmp = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        tmp.close()
        try:
            operator = _FakeOperator()
            updater = AllocationConfigUpdater()
            config = {
                "strategies": [
                    {"strategy_id": "btc_eth", "config_path": tmp.name},
                ]
            }
            ok = asyncio.run(
                updater._update_strategy_configs(self._make_result(), config, operator)
            )

            self.assertTrue(ok)
            adjustments = operator.calls[0][1]
            self.assertEqual(
                adjustments["capital_limits"]["allocation_month"], "2026-10"
            )
            self.assertEqual(
                adjustments["position_sizing.total.account_ratio_cap"], 0.30
            )
        finally:
            os.unlink(tmp.name)


class TestConsumerMonthContract(unittest.TestCase):
    """AC-1.5：消费方按当前月查询，与生效月写入口径对齐，无需改动"""

    def test_daily_refresher_queries_current_active_month(self):
        """daily_refresher SQL 按 month=$1 + active 查询当月记录"""
        sql = " ".join(DailyAllocationRefresher._FETCH_ACTIVE_QUERY.split())
        self.assertIn("WHERE month = $1 AND status = 'active'", sql)

    def test_capital_manager_current_month_equals_effective_month(self):
        """10 月运行时 capital_manager 当前月 = 2026-10，即 9/30 运行写入的生效月"""
        from shared.capital_manager import CapitalManager

        _FrozenDateTime.frozen = datetime(2026, 10, 9, 12, 0, tzinfo=_CST)
        with patch("shared.capital_manager.datetime", _FrozenDateTime):
            self.assertEqual(CapitalManager._current_month(), "2026-10")


class TestManualTriggerArgparse(unittest.TestCase):
    """AC-3：scripts/manual_allocation_trigger.py 的 argparse 支持"""

    @classmethod
    def setUpClass(cls):
        script_path = _PROJECT_ROOT / "scripts" / "manual_allocation_trigger.py"
        spec = importlib.util.spec_from_file_location(
            "manual_allocation_trigger_under_test", script_path
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_default_args_auto_resolve(self):
        """无参数：两个月份均为 None（运行时自动推导）"""
        args = self.module._parse_args([])
        self.assertIsNone(args.pnl_month)
        self.assertIsNone(args.effective_month)

    def test_explicit_months_parsed(self):
        """补生成参数：--pnl-month/--effective-month 正确解析"""
        args = self.module._parse_args(
            ["--pnl-month", "2026-09", "--effective-month", "2026-10"]
        )
        self.assertEqual(args.pnl_month, "2026-09")
        self.assertEqual(args.effective_month, "2026-10")

    def test_unknown_arg_exits(self):
        """未知参数：argparse 拒绝（SystemExit）"""
        with self.assertRaises(SystemExit):
            self.module._parse_args(["--unknown-flag"])


if __name__ == "__main__":
    unittest.main()
