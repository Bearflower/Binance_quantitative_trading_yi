"""export.py 导出 CLI 测试（任务 B：零测试硬缺口补齐）

覆盖场景：
  1. 正常导出：events + manifest 正确写出 JSONL/JSON
  2. DB 连接失败：非 0 退出码（raise SystemExit / 抛异常）
  3. 查询异常（read 路径）：异常上抛，不得被吞成空列表
  4. find_run_by_id 返回 None：显式 SystemExit("run_id 不存在")
  5. finally 块 disconnect() 抛异常：不影响主流程（structlog warning 降级）

所有 mock 目标均从 repository.py / export.py 真实引用核实，禁止凭想象 mock 不存在的方法。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategies.btc_eth.shadow.export import export_run, _async_main, main
from strategies.btc_eth.shadow import export as shadow_export_module


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
UTC = timezone.utc


def _mk_mock_db():
    """构造最小 mock DatabaseManager，具备 connect/disconnect/fetch_one/fetch_all。"""
    db = MagicMock()
    db.connect = AsyncMock()
    db.disconnect = AsyncMock()
    db.fetch_one = AsyncMock()
    db.fetch_all = AsyncMock()
    db.execute = AsyncMock()
    db.pool = MagicMock()
    return db


def _sample_run_dict():
    """模拟一条 shadow_runs 查询返回行（与 repository.py SELECT * 对齐）。"""
    return {
        "run_id": "xau-shadow-test-run",
        "instance_id": "mtcps_xau_shadow",
        "symbol": "XAUUSDT",
        "started_at": datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        "ended_at": None,
        "status": "active",
        "initial_virtual_equity_usdt": 300.0,
        "git_commit": "",
        "git_sha": "abc123",
        "config_hashes": {"xau_original": "sha_xxx"},
        "data_cutoff_at": None,
    }


def _sample_events(n=3):
    """模拟 n 条 shadow_decision_events 查询返回行。"""
    base = {
        "run_id": "xau-shadow-test-run",
        "profile_id": "xau_original",
        "symbol": "XAUUSDT",
        "scheduled_at": datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        "evaluated_at": datetime(2026, 10, 1, 10, 0, 1, tzinfo=UTC),
        "decision": "ENTER",
        "direction": "LONG",
        "grade": "S",
        "score": 0.95,
        "reference_price": 2000.0,
        "intended_order_type": "MARKET",
        "intended_entry_price": None,
        "stop_loss_rule": {"atr_value": 20, "stop_loss_atr_multiplier": 2.0},
        "tp1_rule": None,
        "tp2_rule": None,
        "trailing_rule": None,
        "time_stop_rule": None,
        "feature_snapshot": {"atr": 10, "adx": 25},
        "reason_codes": ["signal_S"],
        "data_quality_reason": None,
        "config_hash": "sha_xxx",
        "git_commit": "",
        "source_kline_close_times": [],
    }
    out = []
    for i in range(n):
        row = dict(base)
        row["scheduled_at"] = datetime(2026, 10, 1, 10, i, tzinfo=UTC)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# 1) 正常导出路径
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_export_run_normal_writes_jsonl():
    """正常导出：run_manifest.json + decision_events.jsonl 正确写出。"""
    db = _mk_mock_db()
    db.fetch_one.return_value = _sample_run_dict()
    db.fetch_all.return_value = _sample_events(n=3)

    with tempfile.TemporaryDirectory() as tmpdir:
        await export_run(db=db, run_id="xau-shadow-test-run", output_dir=tmpdir)

        # 仓储层查询方法必须被调用
        assert db.fetch_one.called, "fetch_one 应被调用（查 run）"
        assert db.fetch_all.called, "fetch_all 应被调用（查 events）"

        events_path = os.path.join(tmpdir, "decision_events.jsonl")
        manifest_path = os.path.join(tmpdir, "run_manifest.json")
        assert os.path.isfile(events_path), f"应写出 {events_path}"
        assert os.path.isfile(manifest_path), f"应写出 {manifest_path}"

        # manifest 能被正确解析
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        assert manifest["run_id"] == "xau-shadow-test-run"
        assert manifest["initial_virtual_equity_usdt"] == 300.0
        assert manifest["git_sha"] == "abc123"

        # JSONL 每行一个事件 JSON
        with open(events_path, "r", encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
        assert len(lines) == 3, f"应导出 3 条事件，实际 {len(lines)}"
        first = json.loads(lines[0])
        assert first["symbol"] == "XAUUSDT"
        assert first["decision"] == "ENTER"


@pytest.mark.asyncio
async def test_export_run_empty_events_writes_empty_jsonl():
    """run_id 合法但无事件：仍写出空 JSONL + manifest。"""
    db = _mk_mock_db()
    db.fetch_one.return_value = _sample_run_dict()
    db.fetch_all.return_value = []

    with tempfile.TemporaryDirectory() as tmpdir:
        await export_run(db=db, run_id="xau-shadow-test-run", output_dir=tmpdir)
        assert os.path.isfile(os.path.join(tmpdir, "decision_events.jsonl"))
        assert os.path.isfile(os.path.join(tmpdir, "run_manifest.json"))


# ---------------------------------------------------------------------------
# 2) find_run_by_id 返回 None → 明确 SystemExit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_export_run_missing_run_id_raises_system_exit():
    """run_id 不存在 → export_run 必须 SystemExit（repository 按契约返回 None）。"""
    db = _mk_mock_db()
    db.fetch_one.return_value = None

    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(SystemExit) as exc_info:
            await export_run(db=db, run_id="nonexistent-run", output_dir=tmpdir)
    assert "nonexistent-run" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3) 查询异常必须上抛，不得被吞成空列表
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_export_run_db_exception_on_fetch_one_propagates():
    """fetch_one 抛异常（DB 故障）必须上抛，不得被吞。"""
    db = _mk_mock_db()
    db.fetch_one.side_effect = ConnectionError("DB 不可达")

    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ConnectionError, match="DB 不可达"):
            await export_run(db=db, run_id="xau-shadow-test-run", output_dir=tmpdir)


@pytest.mark.asyncio
async def test_export_run_db_exception_on_fetch_all_propagates():
    """fetch_all 抛异常（DB 故障）必须上抛，不得被吞。"""
    db = _mk_mock_db()
    db.fetch_one.return_value = _sample_run_dict()
    db.fetch_all.side_effect = ConnectionError("DB 不可达")

    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ConnectionError, match="DB 不可达"):
            await export_run(db=db, run_id="xau-shadow-test-run", output_dir=tmpdir)


# ---------------------------------------------------------------------------
# 4) _async_main 集成：connect/disconnect 生命周期 + 异常降级
# ---------------------------------------------------------------------------
# 关键：patch target 必须是 export.py 内部使用的绑定
# （export.py 用 `from .instance import connect_db_from_env`，
#  所以 target 是 strategies.btc_eth.shadow.export.connect_db_from_env）
CONNECT_TARGET = "strategies.btc_eth.shadow.export.connect_db_from_env"


@pytest.mark.asyncio
async def test_async_main_calls_connect_and_disconnect():
    """_async_main 必须先调 connect() 再 export_run，finally 里调 disconnect()。"""
    mock_db = _mk_mock_db()
    mock_db.fetch_one.return_value = _sample_run_dict()
    mock_db.fetch_all.return_value = _sample_events(n=1)

    with tempfile.TemporaryDirectory() as tmpdir, \
         patch(CONNECT_TARGET, return_value=mock_db):
        await _async_main([
            "--run-id", "xau-shadow-test-run",
            "--output-dir", tmpdir,
        ])

    # connect 必须被显式调用（阻断项 2 修复根因：原代码漏调导致 AttributeError）
    mock_db.connect.assert_awaited_once()
    # finally 里 disconnect 必须被调
    mock_db.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_main_connect_failure_is_not_silent():
    """DB 连接失败：异常上抛，不得静默产出零事件文件。"""
    mock_db = _mk_mock_db()
    mock_db.connect.side_effect = ConnectionError("DB 不可达")

    with tempfile.TemporaryDirectory() as tmpdir, \
         patch(CONNECT_TARGET, return_value=mock_db):
        with pytest.raises(ConnectionError, match="DB 不可达"):
            await _async_main([
                "--run-id", "xau-shadow-test-run",
                "--output-dir", tmpdir,
            ])

    # 连接失败后 disconnect 不应被调（connected=False，安全检查跳过）
    mock_db.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_main_disconnect_failure_is_warned_not_re_raised():
    """finally 块 disconnect() 抛异常：不能覆盖主流程结果，只记 warning。"""
    mock_db = _mk_mock_db()
    mock_db.fetch_one.return_value = _sample_run_dict()
    mock_db.fetch_all.return_value = _sample_events(n=1)
    mock_db.disconnect.side_effect = RuntimeError("连接池关闭时异常")

    with tempfile.TemporaryDirectory() as tmpdir, \
         patch(CONNECT_TARGET, return_value=mock_db):
        # 主流程应正常完成，disconnect 的异常被 logger.warning 降级
        await _async_main([
            "--run-id", "xau-shadow-test-run",
            "--output-dir", tmpdir,
        ])
        # 产物必须存在（disconnect 异常不应影响主流程）
        assert os.path.isfile(os.path.join(tmpdir, "decision_events.jsonl"))
        assert os.path.isfile(os.path.join(tmpdir, "run_manifest.json"))
