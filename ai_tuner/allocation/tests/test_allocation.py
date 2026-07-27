"""
资金分配计算器（AllocationCalculator）单元测试

测试目标：
  - AllocationCalculator.calculate() 方法
  - 首月 fallback 分配
  - 非首月排名分配
  - 零资本保护
  - 负收益率处理
  - 风险备用金计算
  - 模块导入验证
  - config.yaml 配置完整性
"""

import os
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Optional

import yaml

# 确保项目根目录在 sys.path 中，以便导入 ai_tuner 模块
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ai_tuner.allocation.allocation_calculator import (
    AllocationCalculator,
    AllocationEntry,
    AllocationResult,
)


class TestAllocationCalculator(unittest.TestCase):
    """AllocationCalculator 单元测试"""

    # ============================================================
    # 辅助方法
    # ============================================================

    def setUp(self):
        """每个测试用例执行前的初始化"""
        self.calc = AllocationCalculator()

    def _make_result(
        self,
        total_capital: float = 1000.0,
        pnl_data: Optional[Dict] = None,
        is_first_month: bool = False,
        fallback_ratios: Optional[Dict] = None,
        fallback_capitals: Optional[Dict] = None,
        rank_ratios: Optional[List] = None,
        reserve_ratio: float = 0.10,
        strategy_names: Optional[Dict] = None,
        month: str = "2026-07",
    ) -> AllocationResult:
        """快捷调用 calculate() 的辅助方法，使用默认参数值"""
        if pnl_data is None:
            pnl_data = {}
        if fallback_ratios is None:
            fallback_ratios = {}
        if fallback_capitals is None:
            fallback_capitals = {}
        if rank_ratios is None:
            rank_ratios = [0.40, 0.30, 0.20]
        if strategy_names is None:
            strategy_names = {}

        return self.calc.calculate(
            total_capital=total_capital,
            pnl_data=pnl_data,
            is_first_month=is_first_month,
            fallback_ratios=fallback_ratios,
            fallback_capitals=fallback_capitals,
            rank_ratios=rank_ratios,
            reserve_ratio=reserve_ratio,
            strategy_names=strategy_names,
            month=month,
        )

    # ============================================================
    # 测试用例 1：正常排名分配
    # ============================================================

    def test_normal_ranking_allocation(self):
        """
        正常排名分配：3 个策略，收益率不同，按收益率降序排名，
        第1名 40%、第2名 30%、第3名 20%。
        """
        total_capital = 1000.0
        rank_ratios = [0.40, 0.30, 0.20]
        reserve_ratio = 0.10

        # btc_eth: 收益率最高 (300/500=0.6)
        # new_coin: 收益率中等 (100/400=0.25)
        # hrs: 收益率最低 (50/500=0.1)
        pnl_data = {
            "btc_eth": {"pnl": 300.0, "capital": 500.0},
            "new_coin": {"pnl": 100.0, "capital": 400.0},
            "hrs": {"pnl": 50.0, "capital": 500.0},
        }

        strategy_names = {
            "btc_eth": "MTPCS策略",
            "new_coin": "新币做空策略",
            "hrs": "HRS混合反转策略",
        }

        result = self._make_result(
            total_capital=total_capital,
            pnl_data=pnl_data,
            is_first_month=False,
            rank_ratios=rank_ratios,
            reserve_ratio=reserve_ratio,
            strategy_names=strategy_names,
            month="2026-07",
        )

        # 验证基础字段
        self.assertEqual(result.month, "2026-07")
        self.assertEqual(result.total_capital, 1000.0)
        self.assertFalse(result.is_first_month)
        self.assertEqual(len(result.entries), 3)

        # 风险备用金 = 1000 * 0.10 = 100
        self.assertEqual(result.reserve_amount, 100.0)
        # 可分配资金 = 1000 - 100 = 900
        self.assertEqual(result.allocatable_amount, 900.0)

        # 按收益率排序：btc_eth(0.6) > new_coin(0.25) > hrs(0.1)
        entries = result.entries

        # 第 1 名：btc_eth，40%（rank_ratio 为总资金占比）
        self.assertEqual(entries[0].strategy_id, "btc_eth")
        self.assertEqual(entries[0].rank, 1)
        self.assertEqual(entries[0].return_rate, 0.6)
        self.assertEqual(entries[0].allocated_ratio, 0.40)
        self.assertEqual(entries[0].allocated_amount, 400.0)  # 1000 * 0.40 = 400

        # 第 2 名：new_coin，30%
        self.assertEqual(entries[1].strategy_id, "new_coin")
        self.assertEqual(entries[1].rank, 2)
        self.assertEqual(entries[1].return_rate, 0.25)
        self.assertEqual(entries[1].allocated_ratio, 0.30)
        self.assertEqual(entries[1].allocated_amount, 300.0)  # 1000 * 0.30 = 300

        # 第 3 名：hrs，20%
        self.assertEqual(entries[2].strategy_id, "hrs")
        self.assertEqual(entries[2].rank, 3)
        self.assertEqual(entries[2].return_rate, 0.1)
        self.assertEqual(entries[2].allocated_ratio, 0.20)
        self.assertEqual(entries[2].allocated_amount, 200.0)  # 1000 * 0.20 = 200

        # 验证策略名称映射
        self.assertEqual(entries[0].strategy_name, "MTPCS策略")
        self.assertEqual(entries[1].strategy_name, "新币做空策略")
        self.assertEqual(entries[2].strategy_name, "HRS混合反转策略")

    # ============================================================
    # 测试用例 2：首月默认分配
    # ============================================================

    def test_first_month_allocation(self):
        """
        首月分配：使用 fallback_ratios 和 fallback_capitals 分配，
        各策略分配比例与 fallback_ratios 一致。
        """
        fallback_ratios = {
            "btc_eth": 0.30,
            "new_coin": 0.40,
            "hrs": 0.20,
        }
        fallback_capitals = {
            "btc_eth": 300.0,
            "new_coin": 400.0,
            "hrs": 200.0,
        }
        strategy_names = {
            "btc_eth": "MTPCS策略",
            "new_coin": "新币做空策略",
            "hrs": "HRS混合反转策略",
        }

        result = self._make_result(
            total_capital=1000.0,
            pnl_data={},  # 首月无盈亏数据
            is_first_month=True,
            fallback_ratios=fallback_ratios,
            fallback_capitals=fallback_capitals,
            strategy_names=strategy_names,
            month="2026-07",
        )

        self.assertTrue(result.is_first_month)
        self.assertEqual(len(result.entries), 3)

        # 验证各策略分配
        entries = result.entries

        # btc_eth: ratio=0.30, amount=300.0
        self.assertEqual(entries[0].strategy_id, "btc_eth")
        self.assertEqual(entries[0].allocated_ratio, 0.30)
        self.assertEqual(entries[0].allocated_amount, 300.0)

        # new_coin: ratio=0.40, amount=400.0
        self.assertEqual(entries[1].strategy_id, "new_coin")
        self.assertEqual(entries[1].allocated_ratio, 0.40)
        self.assertEqual(entries[1].allocated_amount, 400.0)

        # hrs: ratio=0.20, amount=200.0
        self.assertEqual(entries[2].strategy_id, "hrs")
        self.assertEqual(entries[2].allocated_ratio, 0.20)
        self.assertEqual(entries[2].allocated_amount, 200.0)

        # 首月各策略的盈亏和收益率应为 0
        for entry in entries:
            self.assertEqual(entry.realized_pnl, 0.0)
            self.assertEqual(entry.initial_capital, 0.0)
            self.assertEqual(entry.return_rate, 0.0)

    # ============================================================
    # 测试用例 3：零资本保护
    # ============================================================

    def test_zero_capital_protection(self):
        """
        某策略 capital=0 且 pnl=0 时，return_rate=0.0，
        不抛异常，正常参与排名。
        """
        pnl_data = {
            "btc_eth": {"pnl": 100.0, "capital": 500.0},
            "new_coin": {"pnl": 0.0, "capital": 0.0},  # 零资本
            "hrs": {"pnl": 50.0, "capital": 500.0},
        }
        strategy_names = {
            "btc_eth": "MTPCS策略",
            "new_coin": "新币做空策略",
            "hrs": "HRS混合反转策略",
        }

        result = self._make_result(
            total_capital=1000.0,
            pnl_data=pnl_data,
            is_first_month=False,
            strategy_names=strategy_names,
        )

        # 不应抛异常，应有 3 个条目
        self.assertEqual(len(result.entries), 3)

        # 零资本策略的 return_rate 应为 0.0
        new_coin_entry = [e for e in result.entries if e.strategy_id == "new_coin"][0]
        self.assertEqual(new_coin_entry.return_rate, 0.0)
        self.assertEqual(new_coin_entry.initial_capital, 0.0)
        self.assertEqual(new_coin_entry.realized_pnl, 0.0)

    # ============================================================
    # 测试用例 4：负收益率
    # ============================================================

    def test_negative_return_rate(self):
        """
        某策略 pnl 为负（亏损），return_rate 为负，正确参与排名。
        """
        pnl_data = {
            "btc_eth": {"pnl": 200.0, "capital": 500.0},   # return_rate = 0.4
            "new_coin": {"pnl": -50.0, "capital": 400.0},   # return_rate = -0.125
            "hrs": {"pnl": 100.0, "capital": 500.0},        # return_rate = 0.2
        }
        strategy_names = {
            "btc_eth": "MTPCS策略",
            "new_coin": "新币做空策略",
            "hrs": "HRS混合反转策略",
        }

        result = self._make_result(
            total_capital=1000.0,
            pnl_data=pnl_data,
            is_first_month=False,
            strategy_names=strategy_names,
        )

        entries = result.entries

        # 排名：btc_eth(0.4) > hrs(0.2) > new_coin(-0.125)
        self.assertEqual(entries[0].strategy_id, "btc_eth")
        self.assertEqual(entries[0].rank, 1)
        self.assertEqual(entries[0].return_rate, 0.4)

        self.assertEqual(entries[1].strategy_id, "hrs")
        self.assertEqual(entries[1].rank, 2)
        self.assertEqual(entries[1].return_rate, 0.2)

        self.assertEqual(entries[2].strategy_id, "new_coin")
        self.assertEqual(entries[2].rank, 3)
        self.assertEqual(entries[2].return_rate, -0.125)

        # 验证分配比例：第1名 40%、第2名 30%、第3名 20%
        self.assertEqual(entries[0].allocated_ratio, 0.40)
        self.assertEqual(entries[1].allocated_ratio, 0.30)
        self.assertEqual(entries[2].allocated_ratio, 0.20)

    # ============================================================
    # 测试用例 5：所有策略亏损
    # ============================================================

    def test_all_strategies_losing(self):
        """
        所有策略 pnl 均为负，按收益率（负值）从高到低排名，
        分配比例正常。
        """
        pnl_data = {
            "btc_eth": {"pnl": -10.0, "capital": 500.0},   # return_rate = -0.02
            "new_coin": {"pnl": -50.0, "capital": 400.0},   # return_rate = -0.125
            "hrs": {"pnl": -100.0, "capital": 500.0},       # return_rate = -0.20
        }
        strategy_names = {
            "btc_eth": "MTPCS策略",
            "new_coin": "新币做空策略",
            "hrs": "HRS混合反转策略",
        }

        result = self._make_result(
            total_capital=1000.0,
            pnl_data=pnl_data,
            is_first_month=False,
            strategy_names=strategy_names,
        )

        entries = result.entries

        # 排名：btc_eth(-0.02) > new_coin(-0.125) > hrs(-0.20)
        self.assertEqual(entries[0].strategy_id, "btc_eth")
        self.assertEqual(entries[0].rank, 1)
        self.assertEqual(entries[0].return_rate, -0.02)

        self.assertEqual(entries[1].strategy_id, "new_coin")
        self.assertEqual(entries[1].rank, 2)
        self.assertEqual(entries[1].return_rate, -0.125)

        self.assertEqual(entries[2].strategy_id, "hrs")
        self.assertEqual(entries[2].rank, 3)
        self.assertEqual(entries[2].return_rate, -0.20)

        # 分配比例不受盈亏符号影响（rank_ratio 为总资金占比）
        self.assertEqual(entries[0].allocated_ratio, 0.40)
        self.assertEqual(entries[0].allocated_amount, 400.0)
        self.assertEqual(entries[1].allocated_ratio, 0.30)
        self.assertEqual(entries[1].allocated_amount, 300.0)
        self.assertEqual(entries[2].allocated_ratio, 0.20)
        self.assertEqual(entries[2].allocated_amount, 200.0)

    # ============================================================
    # 测试用例 6：风险备用金计算
    # ============================================================

    def test_reserve_calculation(self):
        """
        验证风险备用金和可分配资金的计算：
        total_capital=1000, reserve_ratio=0.10
        reserve_amount=100, allocatable_amount=900
        """
        result = self._make_result(
            total_capital=1000.0,
            pnl_data={
                "btc_eth": {"pnl": 100.0, "capital": 500.0},
            },
            is_first_month=False,
            reserve_ratio=0.10,
            strategy_names={"btc_eth": "MTPCS策略"},
        )

        self.assertEqual(result.reserve_amount, 100.0)
        self.assertEqual(result.allocatable_amount, 900.0)
        self.assertEqual(result.total_capital, result.reserve_amount + result.allocatable_amount)

    # 补充：不同 reserve_ratio 的边界测试
    def test_reserve_ratio_zero(self):
        """reserve_ratio=0 时，reserve_amount=0，allocatable_amount=total_capital"""
        result = self._make_result(
            total_capital=1000.0,
            pnl_data={
                "btc_eth": {"pnl": 100.0, "capital": 500.0},
            },
            is_first_month=False,
            reserve_ratio=0.0,
            strategy_names={"btc_eth": "MTPCS策略"},
        )

        self.assertEqual(result.reserve_amount, 0.0)
        self.assertEqual(result.allocatable_amount, 1000.0)

    def test_reserve_ratio_one(self):
        """reserve_ratio=1.0 时，reserve_amount=total_capital，allocatable_amount=0"""
        result = self._make_result(
            total_capital=1000.0,
            pnl_data={
                "btc_eth": {"pnl": 100.0, "capital": 500.0},
            },
            is_first_month=False,
            reserve_ratio=1.0,
            strategy_names={"btc_eth": "MTPCS策略"},
        )

        self.assertEqual(result.reserve_amount, 1000.0)
        self.assertEqual(result.allocatable_amount, 0.0)

    # ============================================================
    # 测试用例 7：模块导入验证
    # ============================================================

    def test_module_imports(self):
        """
        验证所有模块可以正常导入。
        """
        from ai_tuner.allocation import (
            AllocationCalculator,
            AllocationEntry,
            AllocationResult,
            AllocationConfigUpdater,
            MonthlyAllocationJob,
            PnLCollector,
        )

        # 确认所有导入的类都存在
        self.assertIsNotNone(AllocationCalculator)
        self.assertIsNotNone(AllocationEntry)
        self.assertIsNotNone(AllocationResult)
        self.assertIsNotNone(AllocationConfigUpdater)
        self.assertIsNotNone(MonthlyAllocationJob)
        self.assertIsNotNone(PnLCollector)

    # ============================================================
    # 测试用例 8：config.yaml 配置完整性
    # ============================================================

    def test_config_yaml_completeness(self):
        """
        验证 config.yaml 中 capital_allocation 配置块存在，
        且所有必需字段存在。
        """
        config_path = _PROJECT_ROOT / "ai_tuner" / "config.yaml"
        self.assertTrue(config_path.exists(), f"配置文件不存在: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        # 验证 capital_allocation 配置块存在
        self.assertIn("capital_allocation", config, "缺少 capital_allocation 配置块")

        ca = config["capital_allocation"]

        # 验证必需字段
        required_fields = [
            "enabled",
            "total_capital",
            "reserve_ratio",
            "rank_ratios",
            "participating_strategies",
            "fallback",
        ]
        for field in required_fields:
            self.assertIn(field, ca, f"capital_allocation 缺少字段: {field}")

        # 验证 enabled 为布尔值
        self.assertIsInstance(ca["enabled"], bool)

        # 验证 total_capital 为正数
        self.assertGreater(ca["total_capital"], 0)

        # 验证 reserve_ratio 在 0~1 之间
        self.assertGreaterEqual(ca["reserve_ratio"], 0.0)
        self.assertLessEqual(ca["reserve_ratio"], 1.0)

        # 验证 rank_ratios 为非空列表
        self.assertIsInstance(ca["rank_ratios"], list)
        self.assertGreater(len(ca["rank_ratios"]), 0)

        # 验证 participating_strategies 为非空列表
        self.assertIsInstance(ca["participating_strategies"], list)
        self.assertGreater(len(ca["participating_strategies"]), 0)

        # 验证 fallback 配置块存在且包含 ratios 和 capitals
        self.assertIn("ratios", ca["fallback"], "fallback 缺少 ratios")
        self.assertIn("capitals", ca["fallback"], "fallback 缺少 capitals")

        # 验证 participating_strategies 中的策略在 fallback.ratios 中都有对应的配置
        for strategy_id in ca["participating_strategies"]:
            self.assertIn(
                strategy_id,
                ca["fallback"]["ratios"],
                f"策略 {strategy_id} 在 fallback.ratios 中缺少配置",
            )


if __name__ == "__main__":
    unittest.main()