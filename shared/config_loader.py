"""
统一配置加载器
负责加载策略的合并配置（基础配置 + AI 调优覆盖层）

核心功能：
- 读取策略 config.yaml 作为基础配置
- 读取 tuning_overrides/.active 获取当前生效版本
- 读取对应的覆盖层 YAML 文件
- deep_merge 合并（覆盖层参数优先）
- 降级策略：覆盖层异常时只加载基础配置，不报错

目录结构：
    strategies/{strategy_id}/
    ├── config.yaml                    # 基础设计参数（AI 永不修改）
    ├── tuning_overrides/              # AI 调优覆盖层目录
    │   ├── .active                    # 内容: "V20260811"（指向当前生效版本）
    │   ├── V20260811.yaml             # 本周调优后生成
    │   ├── V20260804.yaml             # 上周
    │   └── V20260728.yaml             # 上上周
"""

import copy
import hashlib
import json
import os
from typing import Any, Dict, Optional, Tuple

import structlog
import yaml

logger = structlog.get_logger()

# shared 包所在目录（用于定位共享配置文件，如 circuit_breaker_config.yaml）
_SHARED_DIR = os.path.dirname(os.path.abspath(__file__))

# 计算 config_hash 时剔除的敏感键（小写子串匹配：webhook/密钥/令牌/口令）
_SENSITIVE_SUBSTRINGS = ("webhook", "secret", "password", "token", "api_key")

# ============================================================
# 公开 API
# ============================================================


def load_shared_config(yaml_name: str) -> Dict[str, Any]:
    """加载 shared 包内的共享配置文件

    跨策略共享配置（如组合级熔断 shared/circuit_breaker_config.yaml）统一存放于
    shared 目录，经本函数按文件名加载，避免各调用方重复实现读取逻辑。

    Args:
        yaml_name: shared 目录下的 YAML 文件名（如 "circuit_breaker_config.yaml"）

    Returns:
        配置字典；文件不存在、为空或解析失败时返回空字典（降级，不抛异常）。
    """
    config_path = os.path.join(_SHARED_DIR, yaml_name)
    data = _read_yaml(config_path)
    if not data:
        logger.warning(
            "共享配置文件不存在或为空，返回空配置",
            yaml_name=yaml_name,
            config_path=config_path,
        )
        return {}
    return data


def load_strategy_config(strategy_dir: str) -> Dict[str, Any]:
    """
    加载策略的合并配置（基础配置 + AI 调优覆盖层）

    流程：
    1. 读取 config.yaml 作为基础配置
    2. 读取 tuning_overrides/.active 获取当前生效版本
    3. 读取对应的覆盖层 YAML 文件
    4. deep_merge 合并（覆盖层参数优先）

    降级策略（任一条件满足，只加载基础配置，不报错，仅记录 warning）：
    - tuning_overrides/ 目录不存在
    - .active 文件不存在
    - .active 内容为空
    - .active 指向的版本文件不存在
    - 覆盖层 YAML 解析失败

    Args:
        strategy_dir: 策略目录路径（绝对路径或相对于项目根目录）
                      例如 "strategies/btc_eth" 或 "/app/strategies/btc_eth"

    Returns:
        合并后的配置字典。基础配置也不存在时返回空字典。
    """
    merged, _metadata = load_strategy_config_with_metadata(strategy_dir)
    return merged


def load_strategy_config_with_metadata(
    strategy_dir: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    加载策略合并配置并返回同源元数据（旧 load_strategy_config 委托本路径）。

    元数据字段：
    - requested_overrides_version: .active 指向的请求版本（无则 None）
    - applied_overrides_version: 实际成功合并的覆盖版本（回退基础配置记 None，
      不可标为已应用）
    - config_hash: 合并配置的 SHA-256（剔除敏感键，UTF-8 + 排序键 + 固定分隔符）
    - load_error: 加载错误说明（无错误为 None）

    Args:
        strategy_dir: 策略目录路径（绝对路径或相对于项目根目录）

    Returns:
        (合并配置, 元数据)；基础配置缺失时配置为空字典。
    """
    components = _load_components(strategy_dir)
    base_config = components["base_config"]
    requested = components["requested_version"]
    override_config = components["override_config"]

    if base_config is None:
        return {}, _build_metadata(None, requested, None, components["load_error"])
    if override_config is None:
        # 覆盖层缺失/解析失败：回退基础配置，applied 记 None
        return copy.deepcopy(base_config), _build_metadata(
            base_config, requested, None, components["load_error"])

    merged = deep_merge(base_config, override_config)
    logger.info(
        "合并配置加载完成",
        strategy_dir=strategy_dir,
        active_version=requested,
        has_overrides=True,
    )
    return merged, _build_metadata(merged, requested, requested, None)


def _load_components(strategy_dir: str) -> Dict[str, Any]:
    """加载基础配置与覆盖层组件，统一各降级路径的错误记录。"""
    config_path = os.path.join(strategy_dir, "config.yaml")
    base_config = _read_yaml(config_path)
    if not base_config:
        logger.warning("基础配置文件不存在或为空，返回空配置", config_path=config_path)
        return {"base_config": None, "requested_version": None,
                "override_config": None,
                "load_error": f"基础配置缺失或为空: {config_path}"}

    override_dir = os.path.join(strategy_dir, "tuning_overrides")
    active_version = _read_active_version(override_dir)
    if not active_version:
        return {"base_config": base_config, "requested_version": None,
                "override_config": None, "load_error": None}

    override_path = os.path.join(override_dir, f"{active_version}.yaml")
    override_config = _read_yaml(override_path)
    if not override_config:
        logger.warning(
            "覆盖层文件不存在或为空，降级为基础配置",
            override_path=override_path,
            active_version=active_version,
        )
        return {"base_config": base_config, "requested_version": active_version,
                "override_config": None,
                "load_error": f"覆盖层缺失或解析失败: {override_path}"}
    return {"base_config": base_config, "requested_version": active_version,
            "override_config": override_config, "load_error": None}


def _build_metadata(config: Optional[Dict[str, Any]], requested: Optional[str],
                    applied: Optional[str], load_error: Optional[str]) -> Dict[str, Any]:
    """组装元数据；config 为 None（基础配置缺失）时 config_hash 记 None。"""
    return {
        "requested_overrides_version": requested,
        "applied_overrides_version": applied,
        "config_hash": compute_config_hash(config) if config is not None else None,
        "load_error": load_error,
    }


def compute_config_hash(config: Dict[str, Any]) -> str:
    """计算配置身份哈希：剔除敏感键后按规范化 JSON 取 SHA-256（UTF-8 十六进制）。"""
    sanitized = _sanitize_for_hash(copy.deepcopy(config))
    serialized = json.dumps(
        sanitized, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _sanitize_for_hash(value: Any) -> Any:
    """递归剔除敏感键（小写子串匹配）；非字典结构原样返回。"""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            key_lower = str(key).lower()
            if any(s in key_lower for s in _SENSITIVE_SUBSTRINGS):
                continue
            cleaned[key] = _sanitize_for_hash(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_for_hash(item) for item in value]
    return value


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    深度合并两个配置字典

    规则：
    1. 递归合并嵌套字典
    2. 覆盖层参数优先于基础配置
    3. 覆盖层不存在的参数，保留基础配置值
    4. 覆盖层值为 None 的参数，保留基础配置值（不删除）
    5. 列表类型直接替换（不合并）
    6. 标量类型直接覆盖
    7. 不修改原始字典（返回新字典）

    Args:
        base: 基础配置字典
        override: 覆盖层配置字典

    Returns:
        合并后的新字典
    """
    merged = {}

    # 先合并所有基础配置的键
    for key, base_value in base.items():
        if key in override:
            override_value = override[key]
            if override_value is None:
                # 覆盖层显式设为 None，保留基础值
                merged[key] = _deep_copy(base_value)
            elif isinstance(base_value, dict) and isinstance(override_value, dict):
                # 都是字典，递归合并
                merged[key] = deep_merge(base_value, override_value)
            else:
                # 覆盖层值优先（标量或列表直接替换）
                merged[key] = _deep_copy(override_value)
        else:
            # 覆盖层没有此键，保留基础值
            merged[key] = _deep_copy(base_value)

    # 添加覆盖层中有但基础中没有的键
    for key, override_value in override.items():
        if key not in base:
            if override_value is not None:
                merged[key] = _deep_copy(override_value)

    return merged


# ============================================================
# 内部辅助函数
# ============================================================


def _read_yaml(file_path: str) -> Optional[Dict[str, Any]]:
    """
    安全读取 YAML 文件

    降级策略：
    - 文件不存在 → 返回 None
    - 文件为空 → 返回空字典
    - 解析失败 → 记录警告日志，返回 None

    Args:
        file_path: YAML 文件路径

    Returns:
        配置字典，失败时返回 None
    """
    try:
        if not os.path.exists(file_path):
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if data is None:
                return {}
            if not isinstance(data, dict):
                logger.warning(
                    "YAML 文件顶层不是字典，跳过",
                    file_path=file_path,
                    actual_type=type(data).__name__,
                )
                return {}
            return data
    except yaml.YAMLError as e:
        logger.warning("YAML 解析失败", file_path=file_path, error=str(e))
        return None
    except PermissionError as e:
        logger.warning("YAML 文件权限不足", file_path=file_path, error=str(e))
        return None
    except Exception as e:
        logger.warning("YAML 文件读取异常", file_path=file_path, error=str(e))
        return None


def _read_active_version(override_dir: str) -> Optional[str]:
    """
    读取 .active 文件获取当前生效的覆盖层版本号

    降级策略（任一条件满足，返回 None）：目录不存在、.active 不存在、
    内容为空、格式异常、读取异常。

    Args:
        override_dir: tuning_overrides 目录路径

    Returns:
        版本号字符串（如 "V20260811"），无法读取时返回 None
    """
    try:
        if not os.path.exists(override_dir):
            return None
        return _read_active_file(os.path.join(override_dir, ".active"))
    except PermissionError as e:
        logger.warning(
            ".active 文件权限不足，降级为基础配置",
            override_dir=override_dir, error=str(e),
        )
        return None
    except Exception as e:
        logger.warning(
            ".active 文件读取失败，降级为基础配置",
            override_dir=override_dir, error=str(e),
        )
        return None


def _read_active_file(active_path: str) -> Optional[str]:
    """读取并校验 .active 内容（V+版本号）；不存在/空/格式错返回 None。"""
    if not os.path.exists(active_path):
        logger.debug("覆盖层目录存在但 .active 文件不存在，降级为基础配置",
                     active_path=active_path)
        return None
    with open(active_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        logger.warning(".active 文件内容为空，降级为基础配置",
                       active_path=active_path)
        return None
    # 验证版本号格式：V + 至少 1 个字符
    if not content.startswith("V") or len(content) < 2:
        logger.warning(
            ".active 文件内容格式异常（预期 V{YYYYMMDD}），降级为基础配置",
            active_path=active_path, content=content)
        return None
    return content


def _deep_copy(value: Any) -> Any:
    """
    对配置值进行深度拷贝

    支持类型：
    - 字典：递归拷贝所有键值对
    - 列表：递归拷贝所有元素
    - 其他类型（标量）：直接返回

    Args:
        value: 任意可拷贝的值

    Returns:
        深度拷贝后的值
    """
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_deep_copy(item) for item in value]
    # 标量类型（int, float, str, bool, None）不可变，直接返回
    return value