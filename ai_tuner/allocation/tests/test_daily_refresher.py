"""
每日分配金额刷新（DailyAllocationRefresher）单元测试

测试目标：
  - 正常刷新：当月有 active 记录 + 净值查询成功，各策略 allocated_amount 正确重算
  - 净值查询返回 None（失败）：不动 DB，保留旧值
  - 币安抛异常：不动 DB，保留旧值
  - 当月无 active 记录：跳过，不写
  - 空 entries / entries 非 dict 容忍
  - cron 触发器从配置读取，时区默认 Asia/Shanghai
"""

import asyncio
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保项目根目录在 sys.path 中，以便导入 ai_tuner 模块
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ai_tuner.allocation.daily_refresher import DailyAllocationRefresher


def _make_config(**overrides) -> Dict[str, Any]:
    """构造测试用最小配置字典"""
    config = {
        "capital_allocation": {
            "enabled": True,
            "daily_refresh_cron": "15 0 * * *",
        },
        "scheduler": {
            "timezone": "Asia/Shanghai",
        },
    }
    # 允许覆盖 capital_allocation 子段
    if "capital_allocation" in overrides:
        config["capital_allocation"].update(overrides["capital_allocation"])
    return config


class FakeBinanceClient:
    """伪造币安客户端"""

    def __init__(self, result: Optional[Any] = None, raise_error: bool = False):
        """
        Args:
            result: get_account_info 返回值；为 None 且不抛异常时模拟缺少 totalMarginBalance
            raise_error: 是否模拟调用抛异常
        """
        self.result = result
        self.raise_error = raise_error
        self.called = False

    async def get_account_info(self):
        """模拟 get_account_info 调用"""
        self.called = True
        if self.raise_error:
            raise RuntimeError("币安接口异常")
        return self.result


class FakeDb:
    """伪造数据库管理器，记录 fetch 与 execute 调用"""

    def __init__(self, row: Optional[Dict[str, Any]] = None):
        """
        Args:
            row: fetch_one 返回值；None 表示当月无 active 记录
        """
        self.row = row
        self.fetch_one_calls: List = []
        self.execute_calls: List = []

    async def fetch_one(self, query: str, *args, **kwargs):
        """记录查询参数并返回预设结果"""
        self.fetch_one_calls.append(args)
        return self.row

    async def execute(self, query: str, *args, **kwargs):
        """记录更新调用，便于断言"""
        self.execute_calls.append(args)


class TestDailyAllocationRefresher(unittest.TestCase):
    """DailyAllocationRefresher 单元测试"""

    def _make_refresher(self, db, binance=None) -> DailyAllocationRefresher:
        """构造测试用 DailyAllocationRefresher 实例"""
        return DailyAllocationRefresher(
            config=_make_config(),
            db_manager=db,
            binance_client=binance,
        )

    # ============================================================
    # 正常刷新
    # ============================================================

    def test_normal_refresh_recalculates_amounts(self):
        """当月有 active 记录 + 净值查询成功，各策略 allocated_amount 正确重算"""
        entries = [
            {"strategy_id": "btc_eth", "allocated_ratio": 0.30, "allocated_amount": 100.0},
            {"strategy_id": "btc_eth_aggressive", "allocated_ratio": 0.25, "allocated_amount": 100.0},
            {"strategy_id": "new_coin", "allocated_ratio": 0.20, "allocated_amount": 100.0},
            {"strategy_id": "hrs", "allocated_ratio": 0.10, "allocated_amount": 100.0},
        ]
        db = FakeDb(row={"entries": json.dumps(entries)})
        binance = FakeBinanceClient(result={"totalMarginBalance": Decimal("2000")})

        refresher = self._make_refresher(db, binance)
        result = asyncio.run(refresher.run_daily_refresh())

        # 应返回刷新结果
        self.assertIsNotNone(result)
        self.assertEqual(result["total_capital"], 2000.0)
        self.assertEqual(result["strategy_count"], 4)

        # 应执行一次 UPDATE
        self.assertEqual(len(db.execute_calls), 1)
        update_args = db.execute_calls[0]
        # 参数顺序：entries_json, total_capital, month
        self.assertEqual(update_args[1], 2000.0)
        self.assertTrue(isinstance(update_args[2], str) and len(update_args[2]) == 7)

        # 校验重算后的 entries
        updated_entries = json.loads(update_args[0])
        expected = {
            "btc_eth": round(2000.0 * 0.30, 2),       # 600.0
            "btc_eth_aggressive": round(2000.0 * 0.25, 2),  # 500.0
            "new_coin": round(2000.0 * 0.20, 2),      # 400.0
            "hrs": round(2000.0 * 0.10, 2),           # 200.0
        }
        for item in updated_entries:
            self.assertEqual(
                item["allocated_amount"],
                expected[item["strategy_id"]],
                f"{item['strategy_id']} 重算金额错误",
            )

    def test_normal_refresh_accepts_parsed_entries(self):
        """asyncpg 可能直接返回已解析的 list 对象，也应正确处理"""
        entries = [
            {"strategy_id": "btc_eth", "allocated_ratio": 0.30, "allocated_amount": 1.0},
            {"strategy_id": "hrs", "allocated_ratio": 0.10, "allocated_amount": 2.0},
        ]
        db = FakeDb(row={"entries": entries})  # 非字符串，直接是 list
        binance = FakeBinanceClient(result={"totalMarginBalance": 1000})

        refresher = self._make_refresher(db, binance)
        asyncio.run(refresher.run_daily_refresh())

        self.assertEqual(len(db.execute_calls), 1)
        updated_entries = json.loads(db.execute_calls[0][0])
        self.assertEqual(updated_entries[0]["allocated_amount"], 300.0)
        self.assertEqual(updated_entries[1]["allocated_amount"], 100.0)

    # ============================================================
    # 净值查询失败
    # ============================================================

    def test_skip_when_balance_none(self):
        """净值查询返回 None（缺 totalMarginBalance）→ 不动 DB，保留旧值"""
        db = FakeDb(row={"entries": json.dumps([
            {"strategy_id": "btc_eth", "allocated_ratio": 0.30, "allocated_amount": 100.0},
        ])})
        binance = FakeBinanceClient(result={"availableBalance": Decimal("800")})

        refresher = self._make_refresher(db, binance)
        result = asyncio.run(refresher.run_daily_refresh())

        self.assertIsNone(result)
        self.assertEqual(len(db.execute_calls), 0, "净值缺失时不应执行 UPDATE")

    def test_skip_when_binance_raises(self):
        """币安抛异常 → 不动 DB，保留旧值"""
        db = FakeDb(row={"entries": json.dumps([
            {"strategy_id": "btc_eth", "allocated_ratio": 0.30, "allocated_amount": 100.0},
        ])})
        binance = FakeBinanceClient(raise_error=True)

        refresher = self._make_refresher(db, binance)
        result = asyncio.run(refresher.run_daily_refresh())

        self.assertIsNone(result)
        self.assertEqual(len(db.execute_calls), 0, "币安异常时不应执行 UPDATE")

    def test_skip_when_no_binance_client(self):
        """无币安客户端 → 不动 DB，保留旧值"""
        db = FakeDb(row={"entries": json.dumps([
            {"strategy_id": "btc_eth", "allocated_ratio": 0.30, "allocated_amount": 100.0},
        ])})

        refresher = self._make_refresher(db, binance=None)
        result = asyncio.run(refresher.run_daily_refresh())

        self.assertIsNone(result)
        self.assertEqual(len(db.execute_calls), 0)

    # ============================================================
    # 当月无 active 记录
    # ============================================================

    def test_skip_when_no_active_record(self):
        """当月无 active 记录 → 跳过，不写"""
        db = FakeDb(row=None)
        binance = FakeBinanceClient(result={"totalMarginBalance": Decimal("2000")})

        refresher = self._make_refresher(db, binance)
        result = asyncio.run(refresher.run_daily_refresh())

        self.assertIsNone(result)
        self.assertEqual(len(db.execute_calls), 0, "无记录时不应执行 UPDATE")

    # ============================================================
    # enabled 门控
    # ============================================================

    def test_skip_when_disabled(self):
        """enabled=False 时跳过刷新，不查询 DB、不调用币安"""
        db = FakeDb(row={"entries": json.dumps([
            {"strategy_id": "btc_eth", "allocated_ratio": 0.30, "allocated_amount": 100.0},
        ])})
        binance = FakeBinanceClient(result={"totalMarginBalance": Decimal("2000")})

        # 构造一个未启用资金分配的 refresher
        config = _make_config(capital_allocation={"enabled": False})
        refresher = DailyAllocationRefresher(
            config=config,
            db_manager=db,
            binance_client=binance,
        )
        result = asyncio.run(refresher.run_daily_refresh())

        self.assertIsNone(result)
        # 不应查询 DB（fetch_one / execute 均不调用）
        self.assertEqual(len(db.fetch_one_calls), 0, "未启用时不应查询 DB")
        self.assertEqual(len(db.execute_calls), 0, "未启用时不应执行 UPDATE")
        # 不应调用币安
        self.assertFalse(binance.called, "未启用时不应调用币安")

    # ============================================================
    # 净资产为 0 或负值
    # ============================================================

    def test_skip_when_balance_zero_or_negative(self):
        """净资产为 0 或负值时视为获取失败，保留旧值（不 UPDATE）"""
        entries = [
            {"strategy_id": "btc_eth", "allocated_ratio": 0.30, "allocated_amount": 100.0},
        ]
        db = FakeDb(row={"entries": json.dumps(entries)})

        # 净资产为 0
        binance_zero = FakeBinanceClient(result={"totalMarginBalance": Decimal("0")})
        refresher_zero = self._make_refresher(db, binance_zero)
        result_zero = asyncio.run(refresher_zero.run_daily_refresh())
        self.assertIsNone(result_zero)

        # 净资产为负
        binance_neg = FakeBinanceClient(result={"totalMarginBalance": Decimal("-500")})
        refresher_neg = self._make_refresher(db, binance_neg)
        result_neg = asyncio.run(refresher_neg.run_daily_refresh())
        self.assertIsNone(result_neg)

        # 两种情况均不执行 UPDATE（虽查询了 active 记录但保留旧值）
        self.assertEqual(len(db.execute_calls), 0, "净资产非正数时不应执行 UPDATE")

    # ============================================================
    # 净资产大于 0
    # ============================================================

    def test_refresh_when_balance_positive(self):
        """净资产正常大于 0 时正常重算并 UPDATE"""
        entries = [
            {"strategy_id": "btc_eth", "allocated_ratio": 0.30, "allocated_amount": 100.0},
            {"strategy_id": "hrs", "allocated_ratio": 0.10, "allocated_amount": 100.0},
        ]
        db = FakeDb(row={"entries": json.dumps(entries)})
        binance = FakeBinanceClient(result={"totalMarginBalance": Decimal("3000")})

        refresher = self._make_refresher(db, binance)
        result = asyncio.run(refresher.run_daily_refresh())

        # 应返回刷新结果并执行 UPDATE
        self.assertIsNotNone(result)
        self.assertEqual(result["total_capital"], 3000.0)
        self.assertEqual(result["strategy_count"], 2)
        self.assertEqual(len(db.execute_calls), 1)

        # 校验重算后的金额
        update_args = db.execute_calls[0]
        self.assertEqual(update_args[1], 3000.0)
        updated_entries = json.loads(update_args[0])
        self.assertEqual(updated_entries[0]["allocated_amount"], 900.0)  # 0.30
        self.assertEqual(updated_entries[1]["allocated_amount"], 300.0)  # 0.10

    # ============================================================
    # entries 异常容忍
    # ============================================================

    def test_skip_when_entries_empty(self):
        """entries 为空列表 → 跳过，不写"""
        db = FakeDb(row={"entries": json.dumps([])})
        binance = FakeBinanceClient(result={"totalMarginBalance": Decimal("2000")})

        refresher = self._make_refresher(db, binance)
        result = asyncio.run(refresher.run_daily_refresh())

        self.assertIsNone(result)
        self.assertEqual(len(db.execute_calls), 0)

    def test_skip_when_entries_invalid_json(self):
        """entries 为非法 JSON 字符串 → 跳过，不写"""
        db = FakeDb(row={"entries": "not-a-json"})
        binance = FakeBinanceClient(result={"totalMarginBalance": Decimal("2000")})

        refresher = self._make_refresher(db, binance)
        result = asyncio.run(refresher.run_daily_refresh())

        self.assertIsNone(result)
        self.assertEqual(len(db.execute_calls), 0)

    def test_tolerate_non_dict_entries(self):
        """entries 含非 dict 元素或缺 allocated_ratio → 容忍，保留原值不清零"""
        entries = [
            {"strategy_id": "btc_eth", "allocated_ratio": 0.30, "allocated_amount": 100.0},
            "not-a-dict",  # 非 dict：应跳过重算
            {"strategy_id": "hrs"},  # 缺 ratio：应保留原值
        ]
        db = FakeDb(row={"entries": entries})
        binance = FakeBinanceClient(result={"totalMarginBalance": Decimal("1000")})

        refresher = self._make_refresher(db, binance)
        result = asyncio.run(refresher.run_daily_refresh())

        # 仍有有效条目，应正常刷新
        self.assertIsNotNone(result)
        self.assertEqual(len(db.execute_calls), 1)

        updated_entries = json.loads(db.execute_calls[0][0])
        # btc_eth 被正确重算
        btc_entry = updated_entries[0]
        self.assertEqual(btc_entry["allocated_amount"], 300.0)
        # 非 dict 条目被保留（原样输出）
        self.assertEqual(updated_entries[1], "not-a-dict")
        # 缺 ratio 的条目 allocated_amount 未被写入（保留 undefined）
        hrs_entry = updated_entries[2]
        self.assertNotIn("allocated_amount", hrs_entry)

    # ============================================================
    # cron 触发器
    # ============================================================

    def test_get_cron_trigger_uses_config(self):
        """cron 触发器从配置读取执行时刻，时区默认 Asia/Shanghai"""
        refresher = self._make_refresher(FakeDb(row=None), binance=None)
        trigger = refresher.get_cron_trigger()

        # 用固定参考时刻计算下一次触发时间，验证为每天 00:15 北京时间
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Shanghai")
        ref = datetime(2026, 1, 15, 10, 0, tzinfo=tz)
        next_fire = trigger.get_next_fire_time(None, ref)
        self.assertEqual((next_fire.hour, next_fire.minute), (0, 15))
        self.assertEqual(str(trigger.timezone), "Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()