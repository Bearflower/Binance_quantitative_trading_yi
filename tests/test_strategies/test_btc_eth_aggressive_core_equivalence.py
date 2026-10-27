"""MTPCS 激进版纯函数等价提取的对账测试。

流程（计划 D3）：
1. 先以实盘实现冻结逐字段金样（``btc_eth_aggressive_golden_expected.json``）；
2. 提取到 ``strategies/btc_eth_aggressive/core/`` 后，实盘与 core **同时**必须
   与冻结值逐字段一致，任何差异都视为提取失败，不得放宽断言。

注意（D4）：夹具是合成 K 线，本测试只证明「提取前后一致」，
**不构成实盘金样对账通过**。
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from tests.test_strategies.fixtures import btc_eth_aggressive_golden as golden


def _diff_keys(expected: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, Any]:
    """返回不一致的键（只取前 20 个，便于定位）。"""
    diffs: Dict[str, Any] = {}
    for key in sorted(expected):
        if key not in actual:
            diffs[key] = {"expected": expected[key], "actual": "<missing>"}
        elif expected[key] != actual[key]:
            diffs[key] = {"expected": expected[key], "actual": actual[key]}
        if len(diffs) >= 20:
            break
    for key in sorted(set(actual) - set(expected)):
        diffs[key] = {"expected": "<missing>", "actual": actual[key]}
        if len(diffs) >= 20:
            break
    return diffs


def test_live_matches_frozen_golden() -> None:
    """实盘实现（提取前基线 / 提取后转发 core）必须与冻结值一致。"""
    expected = golden.read_expected()["live"]
    actual = golden.build_live_targets()
    diffs = _diff_keys(expected, actual)
    assert not diffs, "实盘实现与冻结金样不一致：{0}".format(diffs)


def test_core_matches_frozen_golden() -> None:
    """core 纯函数必须与冻结金样逐字段一致（未提取时跳过）。"""
    core_targets = golden.build_core_targets()
    if core_targets is None:
        pytest.skip("core 尚未提取")
    expected = golden.read_expected()["live"]
    diffs = _diff_keys(expected, core_targets)
    assert not diffs, "core 与冻结金样不一致：{0}".format(diffs)


def test_golden_covers_branches() -> None:
    """确认金样确实覆盖了关键分支（防止「全 False 也算通过」的空转）。"""
    expected = golden.read_expected()["live"]
    assert any(expected["pattern/{0}/bullish_engulfing".format(s)] for s in golden.SCENARIOS)
    assert any(expected["pattern/{0}/divergence".format(s)] for s in golden.SCENARIOS)
    assert any(expected["trend_filter/{0}".format(s)][0] for s in golden.SCENARIOS)
    assert any(not expected["trend_filter/{0}".format(s)][0] for s in golden.SCENARIOS)
    assert expected["time_stop/S/0.0"] is None
    # S 级 max_holding_hours=120：96h 尚未到期（None），120h 触发时间退出
    assert expected["time_stop/S/96.0"] is None
    assert expected["time_stop/S/120.0"] is not None
    assert any(
        expected["time_stop/{0}/200.0".format(g)] is not None for g in ("S", "A", "B", "C")
    )
