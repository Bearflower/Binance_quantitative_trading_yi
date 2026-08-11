"""
策略数据适配器抽象基类
定义所有策略适配器必须实现的统一接口

所有策略适配器继承此基类，实现 collect()、get_current_params()、validate_params()
三个抽象方法，即可被 StratTuneAI 调度引擎自动调用。
"""

import os
from abc import ABC, abstractmethod
from functools import cached_property
from typing import Any, Dict, List

import yaml
from pydantic import BaseModel, Field


# ============================================================
# 数据模型定义
# ============================================================

class StrategyMeta(BaseModel):
    """策略元信息"""
    strategy_id: str = Field(default="", description="策略唯一标识")
    strategy_name: str = Field(default="", description="策略显示名称")
    version: str = Field(default="", description="策略版本号")
    week_start: str = Field(default="", description="本周起始日期（ISO格式）")
    week_end: str = Field(default="", description="本周结束日期（ISO格式）")
    run_duration_hours: float = Field(default=0.0, description="本周运行时长（小时）")


class PerformanceMetrics(BaseModel):
    """绩效指标"""
    total_trades: int = Field(default=0, description="总交易笔数")
    win_count: int = Field(default=0, description="盈利笔数")
    loss_count: int = Field(default=0, description="亏损笔数")
    win_rate: float = Field(default=0.0, description="胜率")
    total_pnl: float = Field(default=0.0, description="总盈亏（USDT）")
    avg_win: float = Field(default=0.0, description="平均盈利（USDT）")
    avg_loss: float = Field(default=0.0, description="平均亏损（USDT）")
    profit_factor: float = Field(default=0.0, description="盈亏比")
    sharpe_approx: float = Field(default=0.0, description="近似夏普比率")


class RiskMetrics(BaseModel):
    """风险指标"""
    max_consecutive_losses: int = Field(default=0, description="最大连续亏损次数")
    current_drawdown_pct: float = Field(default=0.0, description="当前回撤百分比")
    is_circuit_breaker_active: bool = Field(default=False, description="熔断是否激活")
    max_drawdown_pct: float = Field(default=0.0, description="最大回撤百分比")


class DistributionMetrics(BaseModel):
    """分布指标"""
    avg_holding_hours: float = Field(default=0.0, description="平均持仓时长（小时）")
    signal_distribution: Dict[str, int] = Field(
        default_factory=dict, description="信号等级分布（如 {'S': 3, 'A': 5, 'B': 2}）"
    )
    symbol_distribution: Dict[str, int] = Field(
        default_factory=dict, description="交易对分布（如 {'BTCUSDT': 5, 'ETHUSDT': 3}）"
    )


class SimulationMetrics(BaseModel):
    """模拟推演指标（用于半自动策略，如网格策略）

    通过历史K线数据 + 候选参数组合，模拟推演不同参数下的预期表现。
    AI 调优时对比真实成交（PerformanceMetrics）和模拟推演结果，
    判断参数调整方向。
    """
    scenario_name: str = Field(default="", description="场景名称（如'当前配置'/'更密集网格'）")
    symbol: str = Field(default="", description="模拟的交易对")
    market_state: str = Field(default="", description="市场状态（震荡/弱趋势/强趋势等）")
    grid_count: int = Field(default=0, description="网格数量")
    grid_spacing: float = Field(default=0.0, description="网格间距（USDT）")
    price_range_low: float = Field(default=0.0, description="价格区间下限")
    price_range_high: float = Field(default=0.0, description="价格区间上限")
    profit_rate_per_fill: float = Field(default=0.0, description="每格利润率")
    estimated_fills_weekly: int = Field(default=0, description="预估周填充次数")
    estimated_profit_weekly: float = Field(default=0.0, description="预估周利润（USDT）")
    confidence: float = Field(default=0.0, description="模拟置信度 0-1")


class StrategyReport(BaseModel):
    """策略周度体检报告（统一 Schema）"""
    meta: StrategyMeta = Field(default_factory=StrategyMeta, description="策略元信息")
    performance: PerformanceMetrics = Field(default_factory=PerformanceMetrics, description="绩效指标")
    risk: RiskMetrics = Field(default_factory=RiskMetrics, description="风险指标")
    distribution: DistributionMetrics = Field(default_factory=DistributionMetrics, description="分布指标")
    simulation: List[SimulationMetrics] = Field(default_factory=list, description="模拟推演结果（半自动策略使用）")
    anomalies: List[str] = Field(default_factory=list, description="异常事件列表")


# ============================================================
# 抽象基类
# ============================================================

class BaseAdapter(ABC):
    """
    策略数据适配器基类

    所有策略适配器必须继承此类并实现三个抽象方法：
    - collect(): 采集本周策略表现数据
    - get_current_params(): 获取当前可调参数值
    - validate_params(): 校验 AI 建议的参数是否合法

    类属性（子类必须覆盖）：
    - strategy_id: 策略唯一标识
    - strategy_name: 策略显示名称
    - config_path: 策略配置文件路径
    - param_whitelist: AI 可调参数白名单
    """

    # 子类必须覆盖的类属性
    strategy_id: str = ""
    strategy_name: str = ""
    config_path: str = ""
    param_whitelist: List[str] = []

    def __init__(self, db_manager):
        """
        初始化适配器

        Args:
            db_manager: DatabaseManager 实例，用于查询交易记录
        """
        self.db_manager = db_manager

    @abstractmethod
    async def collect(self, week_offset: int = 0) -> StrategyReport:
        """
        采集策略表现数据，生成标准化报告

        Args:
            week_offset: 周偏移量
                - 0（默认）: 当前周（常规调度使用）
                - -1: 上一周（EffectTracker 回填使用）
                - -2: 上上周
                ...

        Returns:
            StrategyReport: 标准化策略报告
        """
        ...

    @abstractmethod
    def get_current_params(self) -> Dict[str, Any]:
        """
        从策略配置文件中读取当前可调参数值

        Returns:
            字典，key 为参数路径（如 "scoring.min_score"），value 为当前值
        """
        ...

    @abstractmethod
    def validate_params(self, adjustments: Dict[str, Any]) -> Dict[str, Any]:
        """
        校验 AI 建议的参数调整是否合法

        Args:
            adjustments: AI 建议的参数调整，格式为 {param_path: {"from": old, "to": new}}

        Returns:
            校验结果字典，包含：
            - valid: bool，是否全部通过校验
            - errors: list，错误信息列表
            - validated: dict，校验后的参数（可能被截断到边界值）
        """
        ...

    @cached_property
    def _system_config(self) -> Dict[str, Any]:
        """
        读取 AI 调优系统自身配置文件（ai_tuner/config.yaml）

        使用 cached_property 缓存，避免每次调用重复读取文件。

        Returns:
            系统配置字典
        """
        # 配置文件路径：ai_tuner/config.yaml
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config.yaml",
        )
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @cached_property
    def _strategy_cfg(self) -> Dict[str, Any]:
        """
        获取当前策略在 ai_tuner/config.yaml 中的注册配置

        从系统配置文件的 strategies 列表中查找匹配 strategy_id 的条目，
        返回该策略的完整配置（含 param_ranges、param_whitelist 等）。

        使用 cached_property 缓存，避免每次调用重复查找。

        Returns:
            策略注册配置字典，未找到匹配项时返回空字典
        """
        for strategy in self._system_config.get("strategies", []):
            if strategy.get("strategy_id") == self.strategy_id:
                return strategy
        return {}

    def get_param_whitelist(self) -> List[str]:
        """返回该策略的参数白名单列表（从 config.yaml 读取）"""
        return self._strategy_cfg.get("param_whitelist", [])

    def get_redline_params(self) -> List[str]:
        """返回该策略的红线参数列表（从 config.yaml 读取）"""
        return self._strategy_cfg.get("redline_params", [])

    def get_param_ranges(self) -> Dict[str, List[float]]:
        """返回该策略的参数范围定义（从 config.yaml 读取）"""
        return self._strategy_cfg.get("param_ranges", {})

    def to_dict(self) -> Dict[str, Any]:
        """
        将适配器信息转为字典（用于日志和调试）

        Returns:
            包含策略基本信息的字典
        """
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "config_path": self.config_path,
            "param_whitelist": self.get_param_whitelist(),
        }