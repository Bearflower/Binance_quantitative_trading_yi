"""
策略适配器模块

提供统一的策略适配接口，屏蔽不同策略间的差异，向上层提供一致的
数据格式（StrategyReport）和参数管理能力。

公开 API：
    - BaseAdapter: 策略适配器抽象基类
    - MTPCSAdapter: MTPCS 策略适配器
    - NewCoinAdapter: 新币做空策略适配器
    - StrategyMeta: 策略元数据模型
    - PerformanceMetrics: 绩效指标模型
    - RiskMetrics: 风险指标模型
    - DistributionMetrics: 分布指标模型
    - StrategyReport: 策略报告模型
"""

from ai_tuner.adapters.base_adapter import (
    BaseAdapter,
    DistributionMetrics,
    PerformanceMetrics,
    RiskMetrics,
    StrategyMeta,
    StrategyReport,
)
from ai_tuner.adapters.mtpcs_adapter import MTPCSAdapter
from ai_tuner.adapters.new_coin_adapter import NewCoinAdapter

__all__ = [
    "BaseAdapter",
    "MTPCSAdapter",
    "NewCoinAdapter",
    "StrategyMeta",
    "PerformanceMetrics",
    "RiskMetrics",
    "DistributionMetrics",
    "StrategyReport",
]