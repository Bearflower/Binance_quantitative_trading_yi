"""HRS 回放适配器：三表历史快照语义与 fail-closed 语义（Spec F6）。"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ai_tuner.replay.adapters.hrs import SNAPSHOT_REQUIRED_KEYS, HrsAdapter
from ai_tuner.replay.broker import SymbolFilters, VirtualBroker
from ai_tuner.replay.data_portal import DataPortal
from ai_tuner.replay.engine import run_replay
from ai_tuner.replay.models import DataBlocked, QualificationBlocked

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


def _snapshot(
    rows: Any = (),
    complete: bool = True,
    as_of: Any = AS_OF,
) -> Dict[str, Any]:
    """构造一条历史快照记录（``as_of`` 支持 datetime 或 ISO 字符串）。"""
    return {"as_of": as_of, "complete": complete, "rows": list(rows)}


def _full_state() -> Dict[str, Dict[str, Any]]:
    return {
        "hrs_positions": _snapshot([{"symbol": SYMBOL, "direction": "LONG"}]),
        "hrs_blacklist": _snapshot([]),
        "hrs_active_symbols": _snapshot([{"symbol": SYMBOL}]),
    }


def _broker() -> VirtualBroker:
    return VirtualBroker(
        filters={
            SYMBOL: SymbolFilters(
                tick_size=Decimal("0.01"),
                step_size=Decimal("0.001"),
                min_notional=Decimal("5"),
            )
        },
        fee_bps=Decimal("5"),
        slippage_bps=Decimal("2"),
        stop_offset_pct=Decimal("0.002"),
        tp_offset_pct=Decimal("0.0015"),
        initial_equity=Decimal("10000"),
        max_leverage=Decimal("0"),
        tp1_close_ratio=Decimal("0.5"),
    )


def test_prepare_succeeds_when_all_state_tables_present(tmp_path):
    """三表快照齐全（含「确证为空」表）→ prepare 成功且 extras 保留快照映射。"""
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
    assert ctx.extras["state"] == state
    assert ctx.extras["state"]["hrs_blacklist"] == {
        "as_of": AS_OF,
        "complete": True,
        "rows": [],
    }
    assert ctx.extras["symbols"] == [SYMBOL]
    assert adapter.strategy_id == "hrs"
    assert adapter.requires_external_snapshot is True
    assert SNAPSHOT_REQUIRED_KEYS == ("as_of", "complete", "rows")
    assert HrsAdapter.REQUIRED_STATE_TABLES == (
        "hrs_positions",
        "hrs_blacklist",
        "hrs_active_symbols",
    )
    assert HrsAdapter.REQUIRED_INTERVALS == ("1h", "4h")


def test_empty_rows_snapshot_does_not_block(tmp_path):
    """``rows: []`` + complete + as_of 一致 → 合法：这是「历史确证为空」的唯一来源。"""
    portal = _portal(tmp_path)
    state: Dict[str, Dict[str, Any]] = {
        name: _snapshot([]) for name in HrsAdapter.REQUIRED_STATE_TABLES
    }
    ctx = HrsAdapter(lambda as_of: state).prepare(portal)
    assert ctx.extras["state"] == state


def test_snapshot_accepts_iso8601_as_of(tmp_path):
    """快照 as_of 允许 ISO 字符串，与 portal.as_of 统一转 datetime 后相等即通过。"""
    portal = _portal(tmp_path)
    state = _full_state()
    state["hrs_blacklist"] = _snapshot([], as_of="2026-10-22T00:00:00Z")
    ctx = HrsAdapter(lambda as_of: state).prepare(portal)
    assert ctx.extras["state"]["hrs_blacklist"]["as_of"] == "2026-10-22T00:00:00Z"


@pytest.mark.parametrize("missing_table", HrsAdapter.REQUIRED_STATE_TABLES)
def test_missing_state_table_raises_data_blocked(tmp_path, missing_table):
    """任一表为 None（历史不可得）→ DataBlocked，gap 精确到表名。"""
    portal = _portal(tmp_path)
    state: Dict[str, Any] = _full_state()
    state[missing_table] = None

    with pytest.raises(DataBlocked) as excinfo:
        HrsAdapter(lambda as_of: state).prepare(portal)
    assert "{0}:missing_at_as_of".format(missing_table) in excinfo.value.gaps


def test_missing_state_table_key_raises_data_blocked(tmp_path):
    """loader 返回的映射缺该表 key → 与值为 None 同等阻断。"""
    portal = _portal(tmp_path)
    state: Dict[str, Any] = _full_state()
    del state["hrs_blacklist"]

    with pytest.raises(DataBlocked) as excinfo:
        HrsAdapter(lambda as_of: state).prepare(portal)
    assert "hrs_blacklist:missing_at_as_of" in excinfo.value.gaps


@pytest.mark.parametrize(
    "snapshot, expected_gap",
    [
        # 值不是 Mapping（旧契约「表名 → 行列表」被显式拒绝）
        (["row"], "hrs_blacklist:invalid_snapshot"),
        # 快照缺 as_of
        ({"complete": True, "rows": []}, "hrs_blacklist:as_of_missing"),
        # as_of 无法解析
        ({"as_of": "not-a-time", "complete": True, "rows": []}, "hrs_blacklist:as_of_invalid"),
        # as_of 与回放时点不一致
        (
            {"as_of": AS_OF - timedelta(hours=1), "complete": True, "rows": []},
            "hrs_blacklist:as_of_mismatch",
        ),
        # 完整性证明缺失
        ({"as_of": AS_OF, "complete": False, "rows": []}, "hrs_blacklist:incomplete_at_as_of"),
        # 缺 rows
        ({"as_of": AS_OF, "complete": True}, "hrs_blacklist:rows_missing"),
        # rows 类型非法
        ({"as_of": AS_OF, "complete": True, "rows": "x"}, "hrs_blacklist:rows_missing"),
    ],
)
def test_snapshot_gap_codes_block(tmp_path, snapshot, expected_gap):
    """单表快照的每类缺口都以精确 gap 码阻断。"""
    portal = _portal(tmp_path)
    state: Dict[str, Any] = _full_state()
    state["hrs_blacklist"] = snapshot

    with pytest.raises(DataBlocked) as excinfo:
        HrsAdapter(lambda as_of: state).prepare(portal)
    assert expected_gap in excinfo.value.gaps


def test_incomplete_snapshot_with_rows_still_blocks(tmp_path):
    """complete=False 时即便 rows 非空也阻断（有行 ≠ 历史时点完整）。"""
    portal = _portal(tmp_path)
    state = _full_state()
    state["hrs_blacklist"] = _snapshot([{"symbol": SYMBOL}], complete=False)

    with pytest.raises(DataBlocked) as excinfo:
        HrsAdapter(lambda as_of: state).prepare(portal)
    assert "hrs_blacklist:incomplete_at_as_of" in excinfo.value.gaps


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


def test_intents_raise_qualification_blocked(tmp_path):
    """决策未接线时 intents 必须显式阻断，绝不返回空意图。"""
    adapter = HrsAdapter(lambda as_of: _full_state())
    ctx = adapter.prepare(_portal(tmp_path))

    with pytest.raises(QualificationBlocked) as excinfo:
        adapter.intents(ctx)
    assert "hrs_decision_not_wired" in excinfo.value.reasons


def test_run_replay_blocks_instead_of_zero_trade_result(tmp_path):
    """端到端：接线前 run_replay 抛 QualificationBlocked，而不是产出零交易成功结果。"""
    adapter = HrsAdapter(lambda as_of: _full_state())

    with pytest.raises(QualificationBlocked) as excinfo:
        run_replay(_portal(tmp_path), adapter, _broker())
    assert "hrs_decision_not_wired" in excinfo.value.reasons


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
