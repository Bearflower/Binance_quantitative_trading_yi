"""静态守护：MTPCS 激进版 strategy.py 的纯计算方法必须转发 core。

动机：S5 等价提取把纯计算从 `strategies/btc_eth_aggressive/strategy.py` 搬到
`strategies/btc_eth_aggressive/core/`，实盘 wrapper 只做形状适配与状态/IO。
若后续有人把公式「抄回」wrapper，实盘与回放会出现双份实现并悄悄分叉。
本测试用 AST 静态断言「目标方法体内确实调用了对应 core 函数」，
不依赖运行期行为，失败信息直接指出缺哪个转发。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Set

import pytest

STRATEGY_PATH = (
    Path(__file__).resolve().parents[2]
    / "strategies"
    / "btc_eth_aggressive"
    / "strategy.py"
)

# 方法名 → 期望出现的 `_core_xxx.func` 调用点（module_alias.function）
FORWARDING_TARGETS: Dict[str, str] = {
    "_calculate_score": "_core_scoring.calculate_score",
    "_calculate_trend_strength_score": "_core_scoring.trend_strength_score",
    "_calculate_pattern_quality_score": "_core_scoring.pattern_quality_score",
    "_detect_bullish_engulfing": "_core_scoring.detect_bullish_engulfing",
    "_detect_bearish_engulfing": "_core_scoring.detect_bearish_engulfing",
    "_detect_breakout_pullback": "_core_scoring.detect_breakout_pullback",
    "_detect_divergence": "_core_scoring.detect_divergence",
    "_calculate_momentum_divergence_score": "_core_scoring.momentum_divergence_score",
    "_check_trend_filter": "_core_filters.check_trend_filter",
    "_bb_touch_votes": "_core_direction.bb_touch_votes",
    "_rsi_extreme_votes": "_core_direction.rsi_extreme_votes",
    "_reversal_pattern_votes": "_core_direction.reversal_pattern_votes",
    "_vote_ranging_direction": "_core_direction.vote_ranging_direction",
    "_check_direction_alignment": "_core_direction.check_direction_alignment",
    "_safe_last_klines_value": "_core_common.safe_last_klines_value",
    "_evaluate_overheat": "_core_filters.evaluate_overheat",
    "_check_overheat_ban": "_core_filters.check_overheat_ban",
    "_apply_overheat_downgrade": "_core_filters.apply_overheat_downgrade",
    "_check_volume_confirm": "_core_filters.check_volume_confirm",
    "_determine_grade": "_core_scoring.determine_grade",
    "_get_grade_risk": "_core_exits.get_grade_risk",
    "_determine_direction": "_core_direction.determine_direction",
    "_check_volume_filter": "_core_filters.check_volume_filter",
    "_calculate_tp_price": "_core_exits.calculate_tp_price",
    "_calculate_stop_price": "_core_exits.calculate_stop_price",
    "_apply_limit_offset": "_core_exits.apply_limit_offset",
    "_build_indicators": "_core_common.build_indicators",
    # 状态/IO wrapper 里保留的纯逻辑转发
    "_check_prohibited_conditions": "_core_filters.check_kline_spike",
    "_check_volatility_regime": "_core_filters.evaluate_volatility_regime",
    "_should_keep_position": "_core_filters.should_keep_position",
    "_calculate_dynamic_trailing_stop": "_core_exits.trailing_stop_price",
    "_check_time_stop": "_core_exits.time_stop_decision",
}

# 转发后不应再出现在这些方法体里的「被搬走的公式」特征
FORBIDDEN_SNIPPETS: Dict[str, str] = {
    "_check_trend_filter": "np.polyfit",
    "_calculate_dynamic_trailing_stop": "allowed_retrace",
    "_check_time_stop": "max_holding_hours + max_review_hours",
    "_check_prohibited_conditions": "kline_spike_6h_pct",
}


def _module_tree() -> ast.Module:
    return ast.parse(STRATEGY_PATH.read_text(encoding="utf-8"))


def _strategy_class(tree: ast.Module) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "BTCEthStrategy":
            return node
    raise AssertionError("未找到 BTCEthStrategy 类定义")


def _method_body_text(node: ast.AST, source_lines: list) -> str:
    """取方法体源码文本（去掉 docstring，避免文档里提到 core 造成假阳性）。"""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if not body:
        return ""
    start = body[0].lineno
    end = body[-1].end_lineno
    return "\n".join(source_lines[start - 1:end])


def _called_core_attrs(node: ast.AST) -> Set[str]:
    """收集方法体内出现的 `_core_x.func(...)` 调用（形如 `_core_x.func`）。"""
    found: Set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id.startswith("_core_"):
                found.add(f"{func.value.id}.{func.attr}")
    return found


def test_strategy_file_exists() -> None:
    assert STRATEGY_PATH.is_file(), "strategy.py 路径失效，请更新本测试的 STRATEGY_PATH"


def test_all_target_methods_forward_to_core() -> None:
    tree = _module_tree()
    cls = _strategy_class(tree)
    methods = {
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    missing_methods = sorted(set(FORWARDING_TARGETS) - set(methods))
    assert not missing_methods, f"strategy.py 缺少方法: {missing_methods}"

    failures = []
    for name, expected in FORWARDING_TARGETS.items():
        called = _called_core_attrs(methods[name])
        if expected not in called:
            failures.append(f"{name} 未转发 {expected}（实际 core 调用: {sorted(called)}）")

    assert not failures, "以下方法与 core 转发约定不符:\n" + "\n".join(failures)


def test_no_duplicate_formula_left_in_wrapper() -> None:
    source = STRATEGY_PATH.read_text(encoding="utf-8")
    tree = _module_tree()
    lines = source.splitlines()
    cls = _strategy_class(tree)
    methods = {
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    failures = []
    for name, snippet in FORBIDDEN_SNIPPETS.items():
        body_text = _method_body_text(methods[name], lines)
        if snippet in body_text:
            failures.append(f"{name} 仍残留被搬走的公式片段: {snippet!r}")

    assert not failures, "以下方法仍存在重复实现:\n" + "\n".join(failures)


def test_trend_filter_ghost_symbol_param_renamed() -> None:
    """`symbol` 在 _check_trend_filter 体内从未使用，已重命名为 `_symbol`。"""
    tree = _module_tree()
    cls = _strategy_class(tree)
    target = next(
        node for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "_check_trend_filter"
    )
    params = [arg.arg for arg in target.args.args]

    assert params[:2] == ["self", "_symbol"], (
        "`_check_trend_filter` 首参应为 `_symbol`（幽灵参数），实际: "
        f"{params}"
    )


def test_whitelist_size_covers_whole_extraction() -> None:
    """白名单必须覆盖 Spec F3 的全部转发目标（防止漏项）。"""
    assert len(FORWARDING_TARGETS) >= 27


@pytest.mark.parametrize(
    "alias",
    ["_core_common", "_core_scoring", "_core_direction", "_core_filters", "_core_exits"],
)
def test_core_alias_imported(alias: str) -> None:
    tree = _module_tree()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "strategies.btc_eth_aggressive.core":
            for item in node.names:
                if item.asname:
                    imported.add(item.asname)
    assert alias in imported, f"strategy.py 未以 {alias} 别名导入对应 core 模块"
