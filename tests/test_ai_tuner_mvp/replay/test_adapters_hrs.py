"""HRS 回放适配器：三表历史时点恢复语义与 fail-closed 语义（Spec F6）。"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ai_tuner.replay.adapters.hrs import HrsAdapter
from ai_tuner.replay.data_portal import DataPortal
from ai_tuner.replay.models import DataBlocked

from .conftest import make_bars, write_snapshot
from .test_adapters_protocol import ADAPTERS_DIR, collect_imports, import_targets

UTC = timezone.utc
AS_OF = datetime(2026, 10, 22, tzinfo=UTC)
WINDOW_START = AS_OF - timedelta(hours=8)
SYMBOL = "XAUUSDT"

PRICES_1H = [
    ("3300", "3310", "3295", "3305"),
    ("3305", "3315", "3300", "3310"),
    ("3310", "3320", "3305", "3312"),
    ("3312", "3325", "3308", "3320"),
    ("3320", "3330", "3315", "3325"),
    ("3325", "3335", "3295", "3300"),
    ("3300", "3312", "3288", "3292"),
    ("3292", "3305", "3280", "3285"),
]

PRICES_4H = [
    ("3300", "3320", "3290", "3315"),
    ("3315", "3335", "3285", "3290"),
]

#: 适配器允许的绝对 import（标准库白名单 + 回放包内模块）
ALLOWED_ABSOLUTE = {
    "__future__",
    "typing",
    "dataclasses",
    "datetime",
    "decimal",
    "collections",
    "collections.abc",
}
ALLOWED_RELATIVE = {"models", "broker", "base"}


def _write_hrs_snapshot(
    tmp_path: Path,
    include_4h_file: bool = True,
    empty_4h: bool = False,
) -> str:
    """写出 HRS 合成快照目录（universe 恒声明 1h/4h）。"""
    directory = str(tmp_path / "snapshot")
    bars_by_key: Dict[str, List[Dict[str, Any]]] = {
        "{0}|1h".format(SYMBOL): make_bars(SYMBOL, "1h", WINDOW_START, PRICES_1H),
    }
    if empty_4h:
        bars_by_key["{0}|4h".format(SYMBOL)] = []
    else:
        bars_by_key["{0}|4h".format(SYMBOL)] = make_bars(
            SYMBOL, "4h", WINDOW_START, PRICES_4H, step_minutes=240
        )
    skip = [] if include_4h_file else ["klines/{0}_4h.jsonl".format(SYMBOL)]
    write_snapshot(
        directory,
        "hrs",
        AS_OF,
        WINDOW_START,
        bars_by_key,
        [SYMBOL],
        ["1h", "4h"],
        skip_files=skip,
    )
    return directory


def _portal(tmp_path: Path, include_4h_file: bool = True, empty_4h: bool = False) -> DataPortal:
    directory = _write_hrs_snapshot(tmp_path, include_4h_file=include_4h_file, empty_4h=empty_4h)
    return DataPortal(directory, "hrs", AS_OF, WINDOW_START)


def _full_state() -> Dict[str, List[Dict[str, Any]]]:
    return {
        "hrs_positions": [{"symbol": SYMBOL, "direction": "LONG"}],
        "hrs_blacklist": [],
        "hrs_active_symbols": [{"symbol": SYMBOL}],
    }


def test_prepare_succeeds_when_all_state_tables_present(tmp_path):
    """三表齐全（含空列表情形）→ prepare 成功且 extras 保留原状态。"""
    portal = _portal(tmp_path)
    state = _full_state()
    seen: List[datetime] = []

    def loader(as_of: datetime):
        seen.append(as_of)
        return state

    adapter = HrsAdapter(loader)
    ctx = adapter.prepare(portal)

    assert seen == [AS_OF]
    assert ctx.strategy_id == "hrs"
    assert ctx.as_of == AS_OF
    assert ctx.universe.strategy_id == "hrs"
    assert ctx.extras["state"] is state
    assert ctx.extras["state"]["hrs_blacklist"] == []
    assert ctx.extras["symbols"] == [SYMBOL]
    assert adapter.strategy_id == "hrs"
    assert adapter.requires_external_snapshot is True
    assert HrsAdapter.REQUIRED_STATE_TABLES == (
        "hrs_positions",
        "hrs_blacklist",
        "hrs_active_symbols",
    )
    assert HrsAdapter.REQUIRED_INTERVALS == ("1h", "4h")


def test_empty_state_tables_do_not_block(tmp_path):
    """三表都为空列表 → 合法（空表 ≠ 不可得），prepare 成功。"""
    portal = _portal(tmp_path)
    state: Dict[str, List[Dict[str, Any]]] = {
        name: [] for name in HrsAdapter.REQUIRED_STATE_TABLES
    }
    ctx = HrsAdapter(lambda as_of: state).prepare(portal)
    assert ctx.extras["state"] is state


@pytest.mark.parametrize("missing_table", HrsAdapter.REQUIRED_STATE_TABLES)
def test_missing_state_table_raises_data_blocked(tmp_path, missing_table):
    """任一表为 None（历史不可得）→ DataBlocked，gap 精确到表名。"""
    portal = _portal(tmp_path)
    state: Dict[str, Any] = {name: [] for name in HrsAdapter.REQUIRED_STATE_TABLES}
    state[missing_table] = None

    with pytest.raises(DataBlocked) as excinfo:
        HrsAdapter(lambda as_of: state).prepare(portal)
    assert "{0}:missing_at_as_of".format(missing_table) in excinfo.value.gaps


def test_missing_4h_kline_file_raises_data_blocked(tmp_path):
    """快照缺 4h interval 文件 → gaps 含 kline_empty。"""
    portal = _portal(tmp_path, include_4h_file=False)
    with pytest.raises(DataBlocked) as excinfo:
        HrsAdapter(lambda as_of: _full_state()).prepare(portal)
    assert "kline_empty:{0}:4h".format(SYMBOL) in excinfo.value.gaps


def test_empty_4h_kline_rows_raise_data_blocked(tmp_path):
    """4h 文件存在但无行 → 同样 kline_empty（空覆盖不得放行）。"""
    portal = _portal(tmp_path, empty_4h=True)
    with pytest.raises(DataBlocked) as excinfo:
        HrsAdapter(lambda as_of: _full_state()).prepare(portal)
    assert "kline_empty:{0}:4h".format(SYMBOL) in excinfo.value.gaps


def _call_names(source: str) -> List[str]:
    """收集源码全部调用名（``a.b()`` 取 ``b``，``f()`` 取 ``f``）。"""
    tree = ast.parse(source)
    names: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.append(func.attr)
        elif isinstance(func, ast.Name):
            names.append(func.id)
    return names


def test_source_has_no_realtime_clock_calls():
    """静态扫描：不存在 datetime.now / utcnow / time.time 等实时补值路径。"""
    path = ADAPTERS_DIR / "hrs.py"
    source = path.read_text(encoding="utf-8")
    calls = _call_names(source)

    assert "datetime.now" not in source
    assert "utcnow" not in source
    assert "time.time(" not in source
    assert "now" not in calls
    assert "utcnow" not in calls
    assert "time" not in calls


def test_source_import_whitelist():
    """import 白名单：仅标准库 + ai_tuner.replay 内部模块，不碰 strategies/backtest。"""
    path = ADAPTERS_DIR / "hrs.py"

    for level, module, _names in collect_imports(path):
        if level:
            assert module in ALLOWED_RELATIVE, "非法相对 import：{0}".format(module)
            continue
        assert (
            module in ALLOWED_ABSOLUTE
            or module == "ai_tuner.replay"
            or module.startswith("ai_tuner.replay.")
        ), "非法绝对 import：{0}".format(module)

    for target in import_targets(path):
        assert not target.lstrip(".").startswith("strategies"), "不得 import {0}".format(target)
        assert not target.lstrip(".").startswith("backtest"), "不得 import {0}".format(target)
