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

import asyncio
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal
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
from ai_tuner.allocation.monthly_job import MonthlyAllocationJob
from ai_tuner.allocation.pnl_collector import PnLCollector


class TestAllocationCalculator(unittest.TestCase):
    """AllocationCalculator 单元测试"""

    # ============================================================
    # 辅助方法
    # ============================================================

    def setUp(self):
        """每个测试用例执行前的初始化"""
        self.calc = AllocationCalculator()

    def _make_job(self, binance_client=None) -> MonthlyAllocationJob:
        """构造测试用 MonthlyAllocationJob 实例"""
        return MonthlyAllocationJob(
            config={"capital_allocation": {}, "strategies": []},
            db_manager=None,
            notification_client=None,
            messenger=None,
            config_operator=None,
            rollback_manager=None,
            binance_client=binance_client,
        )

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
        首月分配：4 个策略，使用 fallback_ratios 按总资金比例计算
        （不传 fallback_capitals，验证 total_capital × ratio 计算路径，避免掩盖 bug）。

        期望：
        - btc_eth: 1000 × 0.35 = 350.0
        - btc_eth_aggressive: 1000 × 0.10 = 100.0
        - new_coin: 1000 × 0.25 = 250.0
        - hrs: 1000 × 0.15 = 150.0
        - 风险备用金 = 1000 × 0.15 = 150.0（reserve_ratio=0.15）
        """
        fallback_ratios = {
            "btc_eth": 0.35,
            "btc_eth_aggressive": 0.10,
            "new_coin": 0.25,
            "hrs": 0.15,
        }
        strategy_names = {
            "btc_eth": "MTPCS策略",
            "btc_eth_aggressive": "MTPCS激进策略",
            "new_coin": "新币做空策略",
            "hrs": "HRS混合反转策略",
        }

        result = self._make_result(
            total_capital=1000.0,
            pnl_data={},  # 首月无盈亏数据
            is_first_month=True,
            fallback_ratios=fallback_ratios,
            reserve_ratio=0.15,
            strategy_names=strategy_names,
            month="2026-07",
        )

        self.assertTrue(result.is_first_month)
        self.assertEqual(len(result.entries), 4)

        # 风险备用金 = 1000 × 0.15 = 150
        self.assertEqual(result.reserve_amount, 150.0)
        # 可分配资金 = 1000 - 150 = 850
        self.assertEqual(result.allocatable_amount, 850.0)

        # 验证各策略分配（金额 = total_capital × ratio）
        entries = result.entries

        # btc_eth: ratio=0.35, amount=350.0
        self.assertEqual(entries[0].strategy_id, "btc_eth")
        self.assertEqual(entries[0].allocated_ratio, 0.35)
        self.assertEqual(entries[0].allocated_amount, 350.0)

        # btc_eth_aggressive: ratio=0.10, amount=100.0
        self.assertEqual(entries[1].strategy_id, "btc_eth_aggressive")
        self.assertEqual(entries[1].allocated_ratio, 0.10)
        self.assertEqual(entries[1].allocated_amount, 100.0)

        # new_coin: ratio=0.25, amount=250.0
        self.assertEqual(entries[2].strategy_id, "new_coin")
        self.assertEqual(entries[2].allocated_ratio, 0.25)
        self.assertEqual(entries[2].allocated_amount, 250.0)

        # hrs: ratio=0.15, amount=150.0
        self.assertEqual(entries[3].strategy_id, "hrs")
        self.assertEqual(entries[3].allocated_ratio, 0.15)
        self.assertEqual(entries[3].allocated_amount, 150.0)

        # 首月各策略的盈亏和收益率应为 0
        for entry in entries:
            self.assertEqual(entry.realized_pnl, 0.0)
            self.assertEqual(entry.initial_capital, 0.0)
            self.assertEqual(entry.return_rate, 0.0)

    # ============================================================
    # 测试用例 2b：4 策略正常排名分配
    # ============================================================

    def test_normal_ranking_allocation_four_strategies(self):
        """
        正常排名分配：4 个策略（激进版独立参与），收益率不同，按收益率降序排名，
        rank_ratios=[0.30, 0.25, 0.20, 0.10]，reserve_ratio=0.15。

        期望：
        - 第1名 btc_eth(0.6): 1000 × 0.30 = 300.0
        - 第2名 btc_eth_aggressive(0.5): 1000 × 0.25 = 250.0
        - 第3名 new_coin(0.25): 1000 × 0.20 = 200.0
        - 第4名 hrs(0.1): 1000 × 0.10 = 100.0
        - 风险备用金 = 150.0，可分配资金 = 850.0
        """
        total_capital = 1000.0
        rank_ratios = [0.30, 0.25, 0.20, 0.10]
        reserve_ratio = 0.15

        # btc_eth: 收益率最高 (300/500=0.6)
        # btc_eth_aggressive: 收益率次高 (250/500=0.5)
        # new_coin: 收益率中等 (100/400=0.25)
        # hrs: 收益率最低 (50/500=0.1)
        pnl_data = {
            "btc_eth": {"pnl": 300.0, "capital": 500.0},
            "btc_eth_aggressive": {"pnl": 250.0, "capital": 500.0},
            "new_coin": {"pnl": 100.0, "capital": 400.0},
            "hrs": {"pnl": 50.0, "capital": 500.0},
        }

        strategy_names = {
            "btc_eth": "MTPCS策略",
            "btc_eth_aggressive": "MTPCS激进策略",
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
        self.assertEqual(len(result.entries), 4)

        # 风险备用金 = 1000 × 0.15 = 150
        self.assertEqual(result.reserve_amount, 150.0)
        # 可分配资金 = 1000 - 150 = 850
        self.assertEqual(result.allocatable_amount, 850.0)

        # 按收益率排序：btc_eth(0.6) > btc_eth_aggressive(0.5) > new_coin(0.25) > hrs(0.1)
        entries = result.entries

        # 第 1 名：btc_eth，30%
        self.assertEqual(entries[0].strategy_id, "btc_eth")
        self.assertEqual(entries[0].rank, 1)
        self.assertEqual(entries[0].return_rate, 0.6)
        self.assertEqual(entries[0].allocated_ratio, 0.30)
        self.assertEqual(entries[0].allocated_amount, 300.0)  # 1000 * 0.30 = 300

        # 第 2 名：btc_eth_aggressive，25%
        self.assertEqual(entries[1].strategy_id, "btc_eth_aggressive")
        self.assertEqual(entries[1].rank, 2)
        self.assertEqual(entries[1].return_rate, 0.5)
        self.assertEqual(entries[1].allocated_ratio, 0.25)
        self.assertEqual(entries[1].allocated_amount, 250.0)  # 1000 * 0.25 = 250

        # 第 3 名：new_coin，20%
        self.assertEqual(entries[2].strategy_id, "new_coin")
        self.assertEqual(entries[2].rank, 3)
        self.assertEqual(entries[2].return_rate, 0.25)
        self.assertEqual(entries[2].allocated_ratio, 0.20)
        self.assertEqual(entries[2].allocated_amount, 200.0)  # 1000 * 0.20 = 200

        # 第 4 名：hrs，10%
        self.assertEqual(entries[3].strategy_id, "hrs")
        self.assertEqual(entries[3].rank, 4)
        self.assertEqual(entries[3].return_rate, 0.1)
        self.assertEqual(entries[3].allocated_ratio, 0.10)
        self.assertEqual(entries[3].allocated_amount, 100.0)  # 1000 * 0.10 = 100

        # 验证策略名称映射
        self.assertEqual(entries[0].strategy_name, "MTPCS策略")
        self.assertEqual(entries[1].strategy_name, "MTPCS激进策略")
        self.assertEqual(entries[2].strategy_name, "新币做空策略")
        self.assertEqual(entries[3].strategy_name, "HRS混合反转策略")

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

    def test_return_rate_zero_when_capital_zero_with_pnl(self):
        """
        分母（capital，即月初实际占用保证金）为 0 且 pnl 非 0 时，
        收益率记为 0.0（避免除零）。
        """
        pnl_data = {
            "btc_eth": {"pnl": -100.0, "capital": 0.0},
        }
        result = self._make_result(
            total_capital=1000.0,
            pnl_data=pnl_data,
            is_first_month=False,
            strategy_names={"btc_eth": "MTPCS策略"},
        )

        entries = result.entries
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.realized_pnl, -100.0)
        self.assertEqual(entry.initial_capital, 0.0)
        self.assertEqual(entry.return_rate, 0.0)

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

        # 验证 fallback 配置块存在且包含 ratios（capitals 可选，缺失时按比例计算）
        self.assertIn("ratios", ca["fallback"], "fallback 缺少 ratios")
        if "capitals" in ca["fallback"]:
            for strategy_id in ca["participating_strategies"]:
                self.assertIn(
                    strategy_id,
                    ca["fallback"]["capitals"],
                    f"策略 {strategy_id} 在 fallback.capitals 中缺少配置",
                )

        # 验证 participating_strategies 中的策略在 fallback.ratios 中都有对应的配置
        for strategy_id in ca["participating_strategies"]:
            self.assertIn(
                strategy_id,
                ca["fallback"]["ratios"],
                f"策略 {strategy_id} 在 fallback.ratios 中缺少配置",
            )


    # ============================================================
    # 测试用例 9：_get_actual_balance 使用合约账户净资产
    # ============================================================

    def test_get_actual_balance_uses_net_asset(self):
        """
        验证 _get_actual_balance 使用 get_account_info().totalMarginBalance
        作为净资产（accountEquity），而非可用余额（availableBalance）。
        """
        class FakeBinanceClient:
            """伪造币安客户端：记录调用，返回净资产"""

            def __init__(self):
                self.get_account_balance_called = False
                self.get_account_info_called = False

            async def get_account_balance(self):
                # 标记：不应被调用
                self.get_account_balance_called = True
                return {"USDT": Decimal("800")}  # 可用余额（不应被使用）

            async def get_account_info(self):
                self.get_account_info_called = True
                # 净资产 = 可用 + 持仓保证金 + 未实现盈亏
                return {
                    "totalMarginBalance": Decimal("1200"),  # accountEquity 净资产
                    "availableBalance": Decimal("800"),      # 可用余额
                }

        fake_client = FakeBinanceClient()
        job = self._make_job(binance_client=fake_client)

        amount = asyncio.run(job._get_actual_balance())

        # 应使用净资产 totalMarginBalance，而非可用余额
        self.assertEqual(amount, 1200.0)
        self.assertTrue(fake_client.get_account_info_called)
        self.assertFalse(
            fake_client.get_account_balance_called,
            "不应调用 get_account_balance（可用余额口径）",
        )

    def test_get_actual_balance_fallback_when_no_client(self):
        """无 binance_client 时返回 None，使用配置值兜底"""
        job = self._make_job(binance_client=None)

        self.assertIsNone(asyncio.run(job._get_actual_balance()))

    def test_get_actual_balance_missing_total_margin(self):
        """账户信息缺少 totalMarginBalance 字段时返回 None（语义与失败一致）"""
        class FakeBinanceClientMissingField:
            """伪造币安客户端：账户信息缺少 totalMarginBalance"""

            async def get_account_info(self):
                return {"availableBalance": Decimal("800")}

        job = self._make_job(binance_client=FakeBinanceClientMissingField())

        self.assertIsNone(asyncio.run(job._get_actual_balance()))


# ============================================================
# PnLCollector 单元测试（覆盖本次 PnL 采集修复）
# ============================================================

class TestPnLCollector(unittest.TestCase):
    """PnLCollector 单元测试"""

    def _make_collector(self, strategies):
        """构造 PnLCollector 实例（db_manager 可后续替换）"""
        return PnLCollector(db_manager=None, strategies=strategies)

    # ------------------------------------------------------------
    # _resolve_db_names：策略配置名 -> trade_records 落库中文名
    # ------------------------------------------------------------

    def test_resolve_db_names_uses_config_name(self):
        """配置名与落库名一致时直接使用配置名（如 btc_eth）"""
        strategy_cfg = {"strategy_id": "btc_eth", "name": "MTPCS策略"}
        collector = self._make_collector([strategy_cfg])
        self.assertEqual(collector._resolve_db_names(strategy_cfg), ["MTPCS策略"])

    def test_resolve_db_names_hrs_override(self):
        """hrs 配置名(HRS混合反转策略)与落库名(HRS策略)不一致，走显式映射"""
        strategy_cfg = {"strategy_id": "hrs", "name": "HRS混合反转策略"}
        collector = self._make_collector([strategy_cfg])
        self.assertEqual(collector._resolve_db_names(strategy_cfg), ["HRS策略"])

    def test_resolve_db_names_empty_name(self):
        """name 为空时返回空列表（避免误查导致 PnL 恒 0）"""
        strategy_cfg = {"strategy_id": "btc_eth_aggressive", "name": ""}
        collector = self._make_collector([strategy_cfg])
        self.assertEqual(collector._resolve_db_names(strategy_cfg), [])

    # ------------------------------------------------------------
    # _query_strategy_pnl：按落库名列表查询当月已实现盈亏
    # ------------------------------------------------------------

    def test_query_strategy_pnl_empty_names_returns_zero(self):
        """落库名列表为空时直接返回 0.0，不执行数据库查询"""
        class FakeDb:
            """伪造数据库：若被调用则标记失败"""

            def __init__(self):
                self.called = False

            async def fetch_one(self, *args, **kwargs):
                self.called = True
                return {"total_pnl": 100.0}

        fake_db = FakeDb()
        collector = PnLCollector(db_manager=fake_db, strategies=[])
        pnl = asyncio.run(collector._query_strategy_pnl([], None, None))
        self.assertEqual(pnl, 0.0)
        self.assertFalse(fake_db.called, "空名称列表不应触发数据库查询")

    def test_query_strategy_pnl_passes_db_names(self):
        """查询应传入落库中文名列表（而非 strategy_id），SQL 使用 ANY 匹配"""
        class FakeDb:
            """伪造数据库：记录传入参数并返回固定盈亏"""

            def __init__(self):
                self.query = None
                self.args = None

            async def fetch_one(self, query, *args):
                self.query = query
                self.args = args
                return {"total_pnl": 320.5}

        fake_db = FakeDb()
        collector = PnLCollector(db_manager=fake_db, strategies=[])
        pnl = asyncio.run(
            collector._query_strategy_pnl(
                ["HRS策略"],
                datetime(2026, 9, 1),
                datetime(2026, 10, 1),
            )
        )
        self.assertEqual(pnl, 320.5)
        # 第一个参数应为落库名列表
        self.assertEqual(fake_db.args[0], ["HRS策略"])
        # SQL 应使用 ANY($1::text[]) 匹配多个落库名
        self.assertIn("ANY($1::text[])", fake_db.query)

    # ------------------------------------------------------------
    # _query_month_start_margin：查询月初实际占用保证金（收益率分母）
    # ------------------------------------------------------------

    def _make_margin_db(self, month_row=None, prev_row=None):
        """构造按查询类型区分的伪造数据库：本月查询命中 month_row，回退查询命中 prev_row"""

        class FakeMonthMarginDb:
            """伪造数据库：根据 SQL 关键字区分本月查询与回退查询"""

            def __init__(self):
                self.calls = []

            async def fetch_one(self, query, *args):
                self.calls.append(query)
                if "snapshot_hour >=" in query:
                    return month_row
                return prev_row

        return FakeMonthMarginDb()

    def test_month_start_margin_normal(self):
        """本月存在历史快照时，取本月最早一条快照的 open_margin 作为分母"""
        db = self._make_margin_db(month_row={"open_margin": 79.7})
        collector = PnLCollector(db_manager=db, strategies=[])
        margin = asyncio.run(
            collector._query_month_start_margin("hrs", datetime(2026, 9, 1))
        )
        self.assertEqual(margin, 79.7)
        # 命中本月查询，不应触发回退查询
        self.assertIn("snapshot_hour >=", db.calls[0])
        self.assertEqual(len(db.calls), 1)

    def test_month_start_margin_fallbacks_to_last_period(self):
        """本月无历史快照时，回退取早于本月的最近一条快照的 open_margin"""
        db = self._make_margin_db(month_row=None, prev_row={"open_margin": 80.0})
        collector = PnLCollector(db_manager=db, strategies=[])
        margin = asyncio.run(
            collector._query_month_start_margin("hrs", datetime(2026, 9, 1))
        )
        self.assertEqual(margin, 80.0)
        # 依次为本月查询 + 回退查询
        self.assertEqual(len(db.calls), 2)

    def test_month_start_margin_returns_zero_when_no_history(self):
        """完全无历史快照时返回 0.0（不抛异常，除零保护由上层计算器处理）"""
        db = self._make_margin_db(month_row=None, prev_row=None)
        collector = PnLCollector(db_manager=db, strategies=[])
        margin = asyncio.run(
            collector._query_month_start_margin("hrs", datetime(2026, 9, 1))
        )
        self.assertEqual(margin, 0.0)

    def test_month_start_margin_accepts_aware_datetime(self):
        """传入带时区的 month_start 也可正常查询（内部会转为 naive）"""
        from datetime import timedelta, timezone

        cst = timezone(timedelta(hours=8))
        db = self._make_margin_db(month_row={"open_margin": 100.0})
        collector = PnLCollector(db_manager=db, strategies=[])
        margin = asyncio.run(
            collector._query_month_start_margin(
                "hrs",
                datetime(2026, 9, 1, 0, 0, tzinfo=cst),
            )
        )
        self.assertEqual(margin, 100.0)


if __name__ == "__main__":
    unittest.main()