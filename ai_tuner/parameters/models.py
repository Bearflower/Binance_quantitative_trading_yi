"""
参数清单（ParameterManifest）类型定义

每个策略维护机器可读的参数清单，包括：
- 参数路径、类型、范围、最大变化率、所属参数组、代码读取位置
- 参数状态（可调整 / 红线 / shadow / dead / derived）
- 不变量约束（如 adx_strong > adx_medium）

本模块只定义类型，不做 IO 和校验逻辑。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class ParameterStatus(str, Enum):
    """参数状态枚举"""
    TUNABLE = "tunable"      # 允许 AI 调整
    REDLINE = "redline"      # 红线，禁止调整
    SHADOW = "shadow"        # 影子参数，当前不影响实盘
    DEAD = "dead"            # 配置存在但代码未读取
    DERIVED = "derived"      # 由其他参数计算，禁止直接调整


class ValueType(str, Enum):
    """参数值类型"""
    FLOAT = "float"
    INT = "int"


@dataclass(frozen=True)
class ParameterSpec:
    """
    单个可调参数的规格定义

    Attributes:
        path: 参数在配置中的点分隔路径，如 "strategy.scoring.min_score"
        status: 参数状态（tunable / redline / shadow / dead / derived）
        value_type: 值类型（float / int）
        min_value: 允许的最小值
        max_value: 允许的最大值
        max_change_pct: 单次调整最大变化比例（相对于当前值），如 0.10 = 10%
        group: 所属参数组（如 entry_quality / exit_management）
        live_usage_probe: 代码中读取此参数的位置描述，用于 dead 参数检测
        affects: 该参数影响的绩效指标分组（可选）
    """
    path: str
    status: ParameterStatus
    value_type: ValueType
    min_value: float
    max_value: float
    max_change_pct: float
    group: str
    live_usage_probe: str
    affects: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def can_adjust(self) -> bool:
        """只有 TUNABLE 状态允许调整"""
        return self.status is ParameterStatus.TUNABLE

    def coerce(self, value: Any):
        """
        将输入值按 value_type 强制转换

        禁止布尔值（Python 中 bool 是 int 的子类，必须显式拦截）。
        INT 类型拒绝非整数浮点值。

        Args:
            value: 待转换的值

        Returns:
            转换后的值（float 或 int）

        Raises:
            ValueError: 值类型不匹配或超出范围
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.path}: boolean 或非数值类型非法")
        if self.value_type is ValueType.INT:
            if int(value) != value:
                raise ValueError(f"{self.path}: 需要整数，收到 {value}")
            return int(value)
        return float(value)


@dataclass(frozen=True)
class ParameterManifest:
    """
    策略参数清单（一个 manifest 对应一个策略）

    Attributes:
        strategy_id: 策略唯一标识，如 "btc_eth_aggressive"
        schema_version: manifest 格式版本号
        max_adjustments_per_run: 单次调优最多允许修改的参数数量
        parameters: 参数规格字典，key 为参数路径
        invariants: 不变量约束列表，每项是 {"kind": "greater_than", "left": "...", "right": "..."}
    """
    strategy_id: str
    schema_version: str
    max_adjustments_per_run: int
    parameters: Dict[str, ParameterSpec]
    invariants: List[Dict[str, Any]]


@dataclass(frozen=True)
class ValidatedProposal:
    """
    经过确定性校验后的 LLM 提案

    Attributes:
        decision: "PROPOSE" / "HOLD" / "DATA_ERROR"
        adjustments: 通过校验的调整项，格式 {path: {"from": old, "to": new}}
        errors: 阻断性错误列表，非空时 valid=False
        warnings: 非阻断性警告列表
    """
    decision: str
    adjustments: Dict[str, Dict[str, Any]]
    errors: List[str]
    warnings: List[str]

    @property
    def valid(self) -> bool:
        """无阻断性错误即为合法提案"""
        return not self.errors
