"""API 重试 / 幂等 / 订单终态等待的共享配置读取（R02 + R06）

统一从 shared/api_retry_config.yaml 读取重试次数、超时、延迟、开关等参数，
禁止在业务代码中硬编码（遵守 coding-standards.md）。所有取值均提供内置默认值，
当配置文件缺失或字段缺省时自动降级，不会抛异常。

环境变量覆盖：按“配置路径转大写下划线”命名，例如
    api_retry.enabled                            -> API_RETRY_ENABLED
    api_retry.read.max_retries                   -> API_RETRY_READ_MAX_RETRIES
    order_fill.pm_order_visibility_delay_seconds -> ORDER_FILL_PM_ORDER_VISIBILITY_DELAY_SECONDS
值的类型按对应默认值的类型进行强制转换（bool / int / float / str）。
"""

from __future__ import annotations

import copy
import os
from functools import lru_cache
from typing import Any, Dict, Iterator, Tuple

from .config_loader import deep_merge, load_shared_config

# 共享配置文件名（与 shared/api_retry_config.yaml 对应）
_CONFIG_FILE = "api_retry_config.yaml"

# 内置默认值（配置文件缺失/字段缺省时的兜底，与 YAML 保持一致）
_DEFAULTS: Dict[str, Any] = {
    "api_retry": {
        "enabled": True,
        "read": {"max_retries": 3, "delay_seconds": 1.0, "backoff": 2.0},
        "write": {"max_retries": 0, "retry_after_verify": True},
        "order_verify_max_attempts": 3,
        "order_verify_interval_seconds": 0.5,
        "client_order_id_prefix": "sq",
    },
    "order_fill": {
        "enabled": True,
        "check_interval_seconds": 2,
        "pm_order_visibility_delay_seconds": 0.5,
        "final_state_read_retries": 2,
    },
}

_TRUTHY = {"1", "true", "yes", "on"}


def get_api_retry() -> Dict[str, Any]:
    """返回 api_retry 段配置（含 read/write 子段）的副本。"""
    return copy.deepcopy(_load_merged()["api_retry"])


def get_order_fill() -> Dict[str, Any]:
    """返回 order_fill 段配置的副本。"""
    return copy.deepcopy(_load_merged()["order_fill"])


def is_api_retry_enabled() -> bool:
    """R02 总闸：api_retry.enabled（默认 true）。"""
    return bool(get_api_retry()["enabled"])


def is_order_fill_enabled() -> bool:
    """R06 总闸：order_fill.enabled（默认 true），供策略侧决定是否使用结构化等待助手。"""
    return bool(get_order_fill()["enabled"])


def reset_cache() -> None:
    """清空缓存（测试或运行时热更新配置后调用，使环境变量覆盖立即生效）。"""
    _load_merged.cache_clear()


@lru_cache(maxsize=1)
def _load_merged() -> Dict[str, Any]:
    """加载 YAML → 与默认值深度合并 → 应用环境变量覆盖（结果带缓存）。"""
    raw = load_shared_config(_CONFIG_FILE)
    merged = deep_merge(_DEFAULTS, raw if isinstance(raw, dict) else {})
    _apply_env_overrides(merged)
    return merged


def _apply_env_overrides(config: Dict[str, Any]) -> None:
    """就地按环境变量覆盖配置叶子值（类型依据当前值的类型强制转换）。"""
    for path, current in list(_iter_leaves(config)):
        env_name = path.upper().replace(".", "_")
        raw = os.getenv(env_name)
        if raw is None:
            continue
        try:
            _set_by_path(config, path, _coerce(current, raw))
        except (ValueError, TypeError):
            # 非法环境变量值：保留原值，避免因配置笔误导致启动失败
            continue


def _iter_leaves(node: Any, prefix: str = "") -> Iterator[Tuple[str, Any]]:
    """递归遍历嵌套字典，产出 (点分路径, 叶子值)。"""
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{prefix}{key}"
            yield from _iter_leaves(value, f"{child_path}.")
    else:
        yield prefix.rstrip("."), node


def _set_by_path(config: Dict[str, Any], path: str, value: Any) -> None:
    """按点分路径写入嵌套字典。"""
    keys = path.split(".")
    node = config
    for key in keys[:-1]:
        node = node[key]
    node[keys[-1]] = value


def _coerce(default_value: Any, raw: str) -> Any:
    """按默认值类型将字符串环境变量转换为对应类型。"""
    if isinstance(default_value, bool):
        return raw.strip().lower() in _TRUTHY
    if isinstance(default_value, int):
        return int(raw)
    if isinstance(default_value, float):
        return float(raw)
    return raw