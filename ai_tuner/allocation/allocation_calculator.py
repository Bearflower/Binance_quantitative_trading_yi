"""
资金分配计算器

纯计算逻辑，无副作用。根据各策略当月已实现盈亏排名，计算下月资金分配方案。

计算规则：
    - 首月：使用 fallback 默认比例分配
    - 非首月：按各策略收益率降序排名，按 rank_ratios 分配
    - 风险备用金 = total_capital * reserve_ratio，从总资金池中预留
"""

from dataclasses import dataclass, field
from typing import Dict, List

import structlog

logger = structlog.get_logger()


@dataclass
class AllocationEntry:
    """
    单个策略分配条目

    Attributes:
        strategy_id: 策略唯一标识
        strategy_name: 策略显示名称
        realized_pnl: 当月已实现盈亏（USDT）
        initial_capital: 月初分配资金（USDT）
        return_rate: 月收益率 = realized_pnl / initial_capital
        rank: 收益率排名（1 为最高）
        allocated_ratio: 分配比例（0~1）
        allocated_amount: 分配金额（USDT）
    """
    strategy_id: str
    strategy_name: str
    realized_pnl: float
    initial_capital: float
    return_rate: float
    rank: int
    allocated_ratio: float
    allocated_amount: float


@dataclass
class AllocationResult:
    """
    月度资金分配结果

    Attributes:
        month: 分配月份标识，格式 "YYYY-MM"
        total_capital: 总资金池（USDT）
        reserve_amount: 风险备用金（USDT）
        allocatable_amount: 可分配资金（USDT）
        is_first_month: 是否为首月分配
        entries: 各策略分配条目列表
    """
    month: str
    total_capital: float
    reserve_amount: float
    allocatable_amount: float
    is_first_month: bool
    entries: List[AllocationEntry] = field(default_factory=list)


class AllocationCalculator:
    """
    资金分配计算器

    纯计算逻辑，不涉及数据库或文件操作。
    所有参数从外部传入，保证可测试性和可复用性。
    """

    def calculate(
        self,
        total_capital: float,
        pnl_data: Dict[str, Dict[str, float]],
        is_first_month: bool,
        fallback_ratios: Dict[str, float],
        fallback_capitals: Dict[str, float],
        rank_ratios: List[float],
        reserve_ratio: float,
        strategy_names: Dict[str, str],
        month: str,
    ) -> AllocationResult:
        """
        计算月度资金分配方案

        Args:
            total_capital: 总资金池（USDT）
            pnl_data: 各策略盈亏数据 {strategy_id: {"pnl": float, "capital": float}}
            is_first_month: 是否为首月分配
            fallback_ratios: 首月默认分配比例 {strategy_id: ratio}
            fallback_capitals: 首月默认分配金额 {strategy_id: amount}
            rank_ratios: 非首月排名分配比例，按排名从高到低 [0.40, 0.30, 0.20]
            reserve_ratio: 风险备用金比例（0~1）
            strategy_names: 策略显示名称映射 {strategy_id: name}
            month: 分配月份标识，格式 "YYYY-MM"

        Returns:
            AllocationResult 分配结果
        """
        # 计算风险备用金和可分配资金
        reserve_amount = round(total_capital * reserve_ratio, 2)
        allocatable_amount = round(total_capital - reserve_amount, 2)

        logger.info(
            "开始计算资金分配",
            total_capital=total_capital,
            reserve_amount=reserve_amount,
            allocatable_amount=allocatable_amount,
            is_first_month=is_first_month,
            strategy_count=len(pnl_data),
        )

        if is_first_month:
            entries = self._calculate_first_month(
                allocatable_amount=allocatable_amount,
                fallback_ratios=fallback_ratios,
                fallback_capitals=fallback_capitals,
                strategy_names=strategy_names,
            )
        else:
            entries = self._calculate_by_ranking(
                pnl_data=pnl_data,
                total_capital=total_capital,
                rank_ratios=rank_ratios,
                strategy_names=strategy_names,
            )

        return AllocationResult(
            month=month,
            total_capital=total_capital,
            reserve_amount=reserve_amount,
            allocatable_amount=allocatable_amount,
            is_first_month=is_first_month,
            entries=entries,
        )

    def _calculate_first_month(
        self,
        allocatable_amount: float,
        fallback_ratios: Dict[str, float],
        fallback_capitals: Dict[str, float],
        strategy_names: Dict[str, str],
    ) -> List[AllocationEntry]:
        """
        首月分配：使用 fallback 默认比例

        Args:
            allocatable_amount: 可分配资金（USDT）
            fallback_ratios: 默认分配比例
            fallback_capitals: 默认分配金额
            strategy_names: 策略显示名称映射

        Returns:
            分配条目列表
        """
        entries = []
        rank = 1

        for strategy_id, ratio in fallback_ratios.items():
            # 优先使用 fallback_capitals 中配置的金额，否则按比例计算
            if strategy_id in fallback_capitals:
                amount = fallback_capitals[strategy_id]
            else:
                amount = round(allocatable_amount * ratio, 2)

            entries.append(AllocationEntry(
                strategy_id=strategy_id,
                strategy_name=strategy_names.get(strategy_id, strategy_id),
                realized_pnl=0.0,
                initial_capital=0.0,
                return_rate=0.0,
                rank=rank,
                allocated_ratio=ratio,
                allocated_amount=amount,
            ))
            rank += 1

            logger.info(
                "首月分配（默认基准）",
                strategy_id=strategy_id,
                ratio=ratio,
                amount=amount,
            )

        return entries

    def _calculate_by_ranking(
        self,
        pnl_data: Dict[str, Dict[str, float]],
        total_capital: float,
        rank_ratios: List[float],
        strategy_names: Dict[str, str],
    ) -> List[AllocationEntry]:
        """
        非首月分配：按收益率排名分配

        Args:
            pnl_data: 各策略盈亏数据
            total_capital: 总资金池（USDT）
            rank_ratios: 排名分配比例（占总资金百分比）
            strategy_names: 策略显示名称映射

        Returns:
            分配条目列表（按收益率降序排列）
        """
        # 计算各策略收益率
        strategy_returns = []
        for strategy_id, data in pnl_data.items():
            pnl = data.get("pnl", 0.0)
            capital = data.get("capital", 0.0)

            # 计算收益率：若 capital 为 0，收益率为 0（避免除零）
            return_rate = pnl / capital if capital > 0 else 0.0

            strategy_returns.append({
                "strategy_id": strategy_id,
                "pnl": pnl,
                "capital": capital,
                "return_rate": return_rate,
            })

        # 按收益率降序排名
        strategy_returns.sort(key=lambda x: x["return_rate"], reverse=True)

        entries = []
        for rank, s in enumerate(strategy_returns, start=1):
            strategy_id = s["strategy_id"]

            # 获取该排名对应的分配比例
            if rank <= len(rank_ratios):
                ratio = rank_ratios[rank - 1]
            else:
                ratio = 0.0
                logger.warning(
                    "策略超出排名分配范围，分配比例为 0",
                    strategy_id=strategy_id,
                    rank=rank,
                    max_rank=len(rank_ratios),
                )

            amount = round(total_capital * ratio, 2)

            entries.append(AllocationEntry(
                strategy_id=strategy_id,
                strategy_name=strategy_names.get(strategy_id, strategy_id),
                realized_pnl=s["pnl"],
                initial_capital=s["capital"],
                return_rate=s["return_rate"],
                rank=rank,
                allocated_ratio=ratio,
                allocated_amount=amount,
            ))

            logger.info(
                "排名分配",
                strategy_id=strategy_id,
                rank=rank,
                return_rate=round(s["return_rate"], 4),
                ratio=ratio,
                amount=amount,
            )

        return entries