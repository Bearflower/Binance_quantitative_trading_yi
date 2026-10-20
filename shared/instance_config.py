"""实例配置解析模块

为 btc_eth / btc_eth_aggressive 和未来的影子实例提供统一的
配置解析入口，避免两处实现重复。

解析优先级：环境变量 > config['instance'] > 调用方传入的生产默认值。
当 execution_mode == 'shadow_batch' 时，强制 real_ordering_enabled=False、
state_reporting_enabled=False（双开关 fail-closed）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict


# 环境变量名常量（禁止散落在代码各处）
_ENV_INSTANCE_ID = "INSTANCE_ID"
_ENV_EXECUTION_MODE = "EXECUTION_MODE"
_ENV_CAPITAL_STRATEGY_ID = "CAPITAL_STRATEGY_ID"
_ENV_CIRCUIT_BREAKER_POOL = "CIRCUIT_BREAKER_POOL"
_ENV_STATE_REPORTING = "STATE_REPORTING_ENABLED"
_ENV_REAL_ORDERING = "REAL_ORDERING_ENABLED"
_ENV_CONFIG_PATH = "INSTANCE_CONFIG_PATH"


@dataclass
class InstanceConfig:
    """实例运行时配置（所有字段禁止硬编码）

    调用方在调用时传入各自的生产默认值（原版 btc_eth、激进版 btc_eth_aggressive），
    若 config['instance'] 缺省或环境变量未覆盖，则使用这些默认值。
    """

    id: str                      # 实例唯一标识
    execution_mode: str          # live / shadow_batch / ...
    capital_strategy_id: str     # 资金分配管理器 strategy_id
    circuit_breaker_pool: str    # 组合级熔断器 pool 名
    state_reporting_enabled: bool  # 是否调用 save_strategy_state / sync_open_positions
    real_ordering_enabled: bool   # 是否允许真实下单（影子强制关闭）
    config_path: str             # 本实例 config.yaml 路径（用于 config_hash）


def _env_bool(name: str, default: bool) -> bool:
    """从环境变量读取布尔值（支持 1/0/true/false 等常见写法）"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def parse_instance_config(
    config: Dict[str, Any],
    *,
    default_id: str,
    default_execution_mode: str = "live",
    default_capital_strategy_id: str | None = None,
    default_circuit_breaker_pool: str = "mtpcs",
    default_state_reporting_enabled: bool = True,
    default_real_ordering_enabled: bool = True,
    default_config_path: str | None = None,
) -> InstanceConfig:
    """统一实例配置解析函数

    Args:
        config: 从 config_loader 加载的原始 config dict
        default_id: 调用方传入的生产默认 instance id（必传）
        default_execution_mode: 生产默认执行模式
        default_capital_strategy_id: 生产默认资金策略 id；None 时同 default_id
        default_circuit_breaker_pool: 生产默认熔断器池名
        default_state_reporting_enabled: 生产默认是否上报状态
        default_real_ordering_enabled: 生产默认是否允许真实下单
        default_config_path: 生产默认 config.yaml 路径

    Returns:
        填充好的 InstanceConfig 对象
    """
    instance_raw: Dict[str, Any] = config.get("instance", {}) or {}

    default_capital_strategy_id = (
        default_capital_strategy_id if default_capital_strategy_id is not None else default_id
    )

    # 每一项：环境变量 > config.yaml > 生产默认值
    id_value = os.getenv(_ENV_INSTANCE_ID) or instance_raw.get("id") or default_id
    execution_mode = (
        os.getenv(_ENV_EXECUTION_MODE)
        or instance_raw.get("execution_mode")
        or default_execution_mode
    )
    capital_strategy_id = (
        os.getenv(_ENV_CAPITAL_STRATEGY_ID)
        or instance_raw.get("capital_strategy_id")
        or default_capital_strategy_id
    )
    circuit_breaker_pool = (
        os.getenv(_ENV_CIRCUIT_BREAKER_POOL)
        or instance_raw.get("circuit_breaker_pool")
        or default_circuit_breaker_pool
    )
    state_reporting_enabled = _env_bool(
        _ENV_STATE_REPORTING,
        instance_raw.get("state_reporting_enabled", default_state_reporting_enabled),
    )
    real_ordering_enabled = _env_bool(
        _ENV_REAL_ORDERING,
        instance_raw.get("real_ordering_enabled", default_real_ordering_enabled),
    )
    config_path = (
        os.getenv(_ENV_CONFIG_PATH)
        or instance_raw.get("config_path")
        or default_config_path
        or ""
    )

    # shadow_batch 强制关闭写路径（§2 fail-closed）
    if execution_mode == "shadow_batch":
        real_ordering_enabled = False
        state_reporting_enabled = False

    return InstanceConfig(
        id=id_value,
        execution_mode=execution_mode,
        capital_strategy_id=capital_strategy_id,
        circuit_breaker_pool=circuit_breaker_pool,
        state_reporting_enabled=state_reporting_enabled,
        real_ordering_enabled=real_ordering_enabled,
        config_path=config_path,
    )
