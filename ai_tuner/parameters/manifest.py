"""
参数清单加载 + 运行时路径解析 + 提案确定性校验

所有校验逻辑都是纯函数，不依赖网络、数据库或 LLM。
"""

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import yaml

from .models import ParameterManifest, ParameterSpec, ParameterStatus, ValidatedProposal, ValueType


def get_path(config: Mapping[str, Any], path: str) -> Any:
    """
    按点分隔路径从嵌套字典中读取值

    Args:
        config: 嵌套配置字典
        path: 点分隔路径，如 "strategy.scoring.min_score"

    Returns:
        路径对应的值；任一层不存在则返回 None
    """
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def set_path(config: dict, path: str, value: Any) -> None:
    """
    按点分隔路径写入嵌套字典（原地修改）

    中间层不存在时自动创建空字典。

    Args:
        config: 待修改的嵌套字典
        path: 点分隔路径
        value: 待写入的值
    """
    parts = path.split(".")
    current = config
    for part in parts[:-1]:
        next_level = current.get(part)
        if not isinstance(next_level, dict):
            next_level = {}
            current[part] = next_level
        current = next_level
    current[parts[-1]] = value


# 支持的不变量类型；加载 manifest 时遇到未知 kind 直接报错
_SUPPORTED_INVARIANTS = {
    "greater_than",
    "greater_than_or_equal",
}


def validate_invariants(
    manifest: ParameterManifest,
    config: Mapping[str, Any],
) -> list:
    """
    检查 manifest 声明的所有不变量

    Args:
        manifest: 参数清单
        config: 当前（或候选）配置字典

    Returns:
        违反不变量的错误信息列表，空列表表示全部通过
    """
    errors = []
    for inv in manifest.invariants:
        kind = inv.get("kind")
        left_path = inv.get("left")
        right_path = inv.get("right")
        if kind not in _SUPPORTED_INVARIANTS:
            errors.append(f"未知不变量类型: {kind}")
            continue
        left_val = get_path(config, left_path)
        right_val = get_path(config, right_path)
        if left_val is None or right_val is None:
            errors.append(f"不变量 {left_path} vs {right_path} 缺少值")
            continue
        if kind == "greater_than" and not left_val > right_val:
            errors.append(f"不变量违反: {left_path}({left_val}) 应 > {right_path}({right_val})")
        elif kind == "greater_than_or_equal" and not left_val >= right_val:
            errors.append(f"不变量违反: {left_path}({left_val}) 应 >= {right_path}({right_val})")
    return errors


class ManifestRegistry:
    """从磁盘加载 YAML manifest 并解析为 ParameterManifest"""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def load(self, strategy_id: str) -> ParameterManifest:
        """
        加载指定策略的 manifest

        Args:
            strategy_id: 策略唯一标识

        Returns:
            解析后的 ParameterManifest

        Raises:
            FileNotFoundError: manifest 文件不存在
            ValueError: manifest 格式错误或包含未知不变量类型
        """
        file_path = self.directory / f"{strategy_id}.yaml"
        raw = yaml.safe_load(file_path.read_text("utf-8"))

        # 先扫一遍不变量类型，发现未知直接拒绝加载
        for inv in raw.get("invariants", []):
            kind = inv.get("kind")
            if kind not in _SUPPORTED_INVARIANTS:
                raise ValueError(
                    f"manifest {strategy_id} 包含未知不变量类型: {kind}"
                )

        specs = {
            path: ParameterSpec(
                path=path,
                status=ParameterStatus(data["status"]),
                value_type=ValueType(data["value_type"]),
                min_value=float(data["min_value"]),
                max_value=float(data["max_value"]),
                max_change_pct=float(data["max_change_pct"]),
                group=data["group"],
                live_usage_probe=data["live_usage_probe"],
                affects=data.get("affects", {}),
            )
            for path, data in raw["parameters"].items()
        }
        return ParameterManifest(
            strategy_id=raw["strategy_id"],
            schema_version=str(raw["schema_version"]),
            max_adjustments_per_run=int(raw["max_adjustments_per_run"]),
            parameters=specs,
            invariants=raw.get("invariants", []),
        )


def validate_proposal(
    manifest: ParameterManifest,
    effective_config: Mapping[str, Any],
    decision: str,
    adjustments: dict,
) -> ValidatedProposal:
    """
    对 LLM 提案做确定性校验

    校验规则（严格安全优先，任何单参数错误都阻断生效）：
    1. HOLD / DATA_ERROR 不得携带 adjustments
    2. PROPOSE 必须携带至少 1 个 adjustment，且不超过 max_adjustments_per_run
    3. 每个 path 必须在 manifest 中且状态为 TUNABLE
    4. from 值必须与 effective_config 中的真实值一致（防 LLM 捏造基线）
    5. to 值必须在 manifest 声明的 [min_value, max_value] 范围
    6. 变化率不得超过 manifest 声明的 max_change_pct
    7. 通过单参数校验后，应用变更构建候选配置，检查不变量

    Args:
        manifest: 策略的 ParameterManifest
        effective_config: 策略当前生效的配置字典（嵌套）
        decision: LLM 输出的决定（PROPOSE / HOLD / DATA_ERROR）
        adjustments: LLM 输出的调整项 {path: {"from": old, "to": new}}

    Returns:
        ValidatedProposal；errors 非空时 adjustments 返回空字典
    """
    errors: list = []
    warnings: list = []
    accepted: dict = {}

    # --- 决策类型 ---
    if decision in {"HOLD", "DATA_ERROR"}:
        if adjustments:
            errors.append(f"{decision} 不得携带 adjustments")
        return ValidatedProposal(decision, {}, errors, warnings)
    if decision != "PROPOSE":
        return ValidatedProposal(decision, {}, [f"不支持的 decision: {decision}"], warnings)

    # --- PROPOSE 必须有调整 ---
    if not adjustments:
        errors.append("PROPOSE 必须包含至少 1 个 adjustment")
    elif len(adjustments) > manifest.max_adjustments_per_run:
        errors.append(
            f"本轮最多允许 {manifest.max_adjustments_per_run} 个 adjustment，"
            f"收到 {len(adjustments)}"
        )

    # --- 逐参数校验 ---
    for path, change in adjustments.items():
        spec = manifest.parameters.get(path)
        if spec is None or not spec.can_adjust:
            errors.append(f"{path}: 不在 manifest 中或不可调整")
            continue

        # coerce 会拦截 bool 和非数值
        try:
            old = spec.coerce(change.get("from"))
            new = spec.coerce(change.get("to"))
        except (TypeError, ValueError) as e:
            errors.append(f"{path}: 值类型错误 - {e}")
            continue

        # from 必须匹配真实当前值（允许 1e-12 浮点容差）
        actual = get_path(effective_config, path)
        if actual is None:
            errors.append(f"{path}: 在当前配置中不存在")
            continue
        try:
            actual_f = float(actual)
        except (TypeError, ValueError):
            errors.append(f"{path}: 当前值非数值，无法校验 from")
            continue
        if abs(float(old) - actual_f) > 1e-12:
            errors.append(
                f"{path}: from 值 {old} 与生效值 {actual} 不匹配"
            )
            continue

        # to 必须在 manifest 范围内
        if not spec.min_value <= float(new) <= spec.max_value:
            errors.append(
                f"{path}: 目标值 {new} 超出范围 [{spec.min_value}, {spec.max_value}]"
            )
            continue

        # 变化率不得超过 max_change_pct（相对于真实当前值）
        denominator = abs(actual_f)
        change_pct = abs(float(new) - actual_f) / denominator if denominator else float("inf")
        if change_pct > spec.max_change_pct + 1e-12:
            errors.append(
                f"{path}: 变化 {change_pct:.1%} 超过上限 {spec.max_change_pct:.0%}"
            )
            continue

        accepted[path] = {"from": old, "to": new}

    # --- 不变量校验 ---
    if not errors:
        candidate = deepcopy(dict(effective_config))
        for path, item in accepted.items():
            set_path(candidate, path, item["to"])
        inv_errors = validate_invariants(manifest, candidate)
        errors.extend(inv_errors)

    final_adjustments = accepted if not errors else {}
    return ValidatedProposal(decision, final_adjustments, errors, warnings)
