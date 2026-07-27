"""
月度资金分配模块

每月末自动执行，基于各策略当月已实现盈亏排名，动态分配下月资金。

公开 API：
    - MonthlyAllocationJob: 月度分配作业，编排完整分配流程
    - PnLCollector: 盈亏采集器，从数据库查询各策略当月已实现盈亏
    - AllocationCalculator: 分配计算器，纯计算逻辑，无副作用
    - AllocationConfigUpdater: 配置更新器，将分配结果写入数据库和配置文件
    - ProfitExtractionJob: 利润提取提醒，每日检查账户权益创新高
    - AllocationResult: 分配结果数据类
    - AllocationEntry: 单个策略分配条目数据类
"""

from ai_tuner.allocation.allocation_calculator import (
    AllocationCalculator,
    AllocationEntry,
    AllocationResult,
)
from ai_tuner.allocation.config_updater import AllocationConfigUpdater
from ai_tuner.allocation.monthly_job import MonthlyAllocationJob
from ai_tuner.allocation.pnl_collector import PnLCollector
from ai_tuner.allocation.profit_extraction_job import ProfitExtractionJob

__all__ = [
    "MonthlyAllocationJob",
    "PnLCollector",
    "AllocationCalculator",
    "AllocationConfigUpdater",
    "ProfitExtractionJob",
    "AllocationResult",
    "AllocationEntry",
]