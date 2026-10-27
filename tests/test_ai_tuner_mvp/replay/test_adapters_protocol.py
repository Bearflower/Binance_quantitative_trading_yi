"""协议一致性守护（Spec F4）：适配器与 engine 的接线契约、import 白名单。

本文件同时提供 ``collect_imports`` 供 ``test_adapters_hrs.py`` 复用（避免重复实现）。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import List, Tuple

import pytest

import ai_tuner.replay.adapters.base as base_module
import ai_tuner.replay.engine as engine_module

ADAPTERS_DIR = Path(base_module.__file__).resolve().parent

#: 适配器绝对 import 的禁止前缀（backtest 一律禁止）
FORBIDDEN_PREFIXES = ("backtest",)

#: MTPCS 严格禁止的具体模块（只允许 core）
MTPCS_FORBIDDEN_TARGETS = ("strategies.btc_eth_aggressive.strategy",)


def collect_imports(path: Path) -> List[Tuple[int, str, Tuple[str, ...]]]:
    """返回模块顶层 import 的 ``(相对层级, 模块名, 导入名元组)`` 列表。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: List[Tuple[int, str, Tuple[str, ...]]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((0, alias.name, ()))
        elif isinstance(node, ast.ImportFrom):
            names = tuple(alias.name for alias in node.names)
            imports.append((node.level or 0, node.module or "", names))
    return imports


def import_targets(path: Path) -> List[str]:
    """把顶层 import 展平成点号全名列表（绝对与相对 import 都展开）。"""
    targets: List[str] = []
    for level, module, names in collect_imports(path):
        prefix = "." * level + module
        if not names:
            targets.append(prefix)
            continue
        for name in names:
            targets.append("{0}.{1}".format(prefix, name) if prefix else name)
    return targets


def test_protective_prices_signature_is_self_and_symbol():
    """协议签名固定为 (self, symbol)，不得带 position 等额外参数。"""
    parameters = list(inspect.signature(base_module.ReplayAdapter.protective_prices).parameters)
    assert parameters == ["self", "symbol"]


def test_engine_calls_protective_prices_with_symbol_only():
    """engine 的唯一调用点必须是 ``adapter.protective_prices(symbol)``。"""
    source = Path(engine_module.__file__).read_text(encoding="utf-8")
    assert "adapter.protective_prices(symbol)" in source

    tree = ast.parse(source, filename=str(engine_module.__file__))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "protective_prices"
    ]
    assert calls, "engine 未调用 protective_prices"
    for call in calls:
        assert len(call.args) == 1, "protective_prices 只能接收一个位置参数"
        argument = call.args[0]
        assert isinstance(argument, ast.Name) and argument.id == "symbol"
        assert not call.keywords, "protective_prices 不得以关键字传参"


@pytest.mark.parametrize("module_name", ["mtpcs_aggressive", "hrs"])
def test_adapters_do_not_import_strategy_or_backtest(module_name):
    """两个适配器的顶层 import 都不得触碰策略入口或 backtest。"""
    path = ADAPTERS_DIR / "{0}.py".format(module_name)
    if not path.is_file():
        if module_name == "mtpcs_aggressive":
            pytest.skip("MTPCS 适配器尚未落地")
        pytest.fail("适配器文件缺失：{0}".format(path))

    targets = import_targets(path)
    for target in targets:
        for prefix in FORBIDDEN_PREFIXES:
            assert not target.startswith(prefix), "{0} import 了 {1}".format(module_name, target)

    if module_name == "mtpcs_aggressive":
        for forbidden in MTPCS_FORBIDDEN_TARGETS:
            for target in targets:
                assert not (target == forbidden or target.startswith(forbidden + ".")), (
                    "MTPCS 适配器不得 import {0}".format(target)
                )
