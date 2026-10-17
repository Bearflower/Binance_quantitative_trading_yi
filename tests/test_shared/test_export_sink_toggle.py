"""
export_execution_facts._maybe_open_sink / _maybe_close_sink 离线测试。

覆盖：
  - 开关默认关闭（环境变量未设置 → (None, None)，不构造 DatabaseManager）
  - truthy 判定：1/true/yes/on（strip+lower，大小写不敏感）；其余一律关闭
  - truthy 时 DB 连接参数从 DATABASE_* 环境变量读取
  - connect 失败 → 降级 (None, None)，不阻塞采集、不向上抛
  - _maybe_close_sink：None 空跑 / 正常 disconnect / disconnect 异常吞掉

DatabaseManager 全程 monkeypatch 为假件，绝不连真实 PostgreSQL。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

import shared.database as shared_database_mod
from scripts.export_execution_facts import _maybe_open_sink, _maybe_close_sink

_ENV = "EXECUTION_FACTS_DB_WRITE"


# ── 假 DatabaseManager：记录构造参数与调用次数，可注入失败 ──────

class FakeDatabaseManager:
    """记录构造 kwargs 与 connect/disconnect 调用的假件。"""

    next_connect_error: Optional[Exception] = None
    next_disconnect_error: Optional[Exception] = None
    instances: List["FakeDatabaseManager"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs: Dict[str, Any] = kwargs
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connect_error = FakeDatabaseManager.next_connect_error
        self.disconnect_error = FakeDatabaseManager.next_disconnect_error
        FakeDatabaseManager.instances.append(self)

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error


@pytest.fixture(autouse=True)
def _reset_fake_state():
    """每个用例前重置假件类状态，测试后恢复（避免类属性跨用例污染）。"""
    FakeDatabaseManager.next_connect_error = None
    FakeDatabaseManager.next_disconnect_error = None
    FakeDatabaseManager.instances = []
    yield
    FakeDatabaseManager.next_connect_error = None
    FakeDatabaseManager.next_disconnect_error = None
    FakeDatabaseManager.instances = []


@pytest.fixture
def fake_db_cls(monkeypatch) -> type:
    """把 shared.database.DatabaseManager 替换为假件（延迟 import 场景下生效）。"""
    monkeypatch.setattr(shared_database_mod, "DatabaseManager", FakeDatabaseManager)
    return FakeDatabaseManager


# ══════════════════════════════════════════════════════════════════
# 开关关闭分支
# ══════════════════════════════════════════════════════════════════

def test_open_sink_disabled_by_default(monkeypatch, fake_db_cls):
    """环境变量未设置 → (None, None)，且完全不构造 DatabaseManager。"""
    monkeypatch.delenv(_ENV, raising=False)
    sink, db = asyncio.run(_maybe_open_sink())
    assert sink is None
    assert db is None
    assert fake_db_cls.instances == []


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "   ", "2", "enabled", "TRUEE"])
def test_open_sink_falsy_values(monkeypatch, fake_db_cls, raw):
    """非 truthy 值（含大小写变体误拼）→ 关闭，不构造 DB。"""
    monkeypatch.setenv(_ENV, raw)
    sink, db = asyncio.run(_maybe_open_sink())
    assert sink is None
    assert db is None
    assert fake_db_cls.instances == []


# ══════════════════════════════════════════════════════════════════
# 开关开启分支
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "True", "yes", "Yes", "on", "ON", " on "])
def test_open_sink_truthy_values(monkeypatch, fake_db_cls, raw):
    """truthy（strip+lower 后命中 1/true/yes/on）→ 开启，sink 持有已连接的 db。"""
    monkeypatch.setenv(_ENV, raw)
    sink, db = asyncio.run(_maybe_open_sink())
    assert sink is not None
    assert db is not None
    assert sink.db is db                      # sink 与 db_manager 同源
    assert db.connect_calls == 1              # connect 恰好一次


def test_open_sink_reads_db_env(monkeypatch, fake_db_cls):
    """DB 连接参数应从 DATABASE_* 环境变量读取（而非硬编码）。"""
    monkeypatch.setenv(_ENV, "1")
    monkeypatch.setenv("DATABASE_HOST", "db-host-test")
    monkeypatch.setenv("DATABASE_PORT", "6543")
    monkeypatch.setenv("DATABASE_NAME", "db-name-test")
    monkeypatch.setenv("DATABASE_USER", "db-user-test")
    monkeypatch.setenv("DATABASE_PASSWORD", "db-pass-test")
    _, db = asyncio.run(_maybe_open_sink())
    assert db.kwargs["host"] == "db-host-test"
    assert db.kwargs["port"] == 6543
    assert db.kwargs["database"] == "db-name-test"
    assert db.kwargs["user"] == "db-user-test"
    assert db.kwargs["password"] == "db-pass-test"


def test_open_sink_connect_failure_degrades(monkeypatch, fake_db_cls):
    """connect 抛异常 → 降级 (None, None)：不向上抛、不阻塞采集。"""
    monkeypatch.setenv(_ENV, "1")
    FakeDatabaseManager.next_connect_error = RuntimeError("connect boom")
    sink, db = asyncio.run(_maybe_open_sink())
    assert sink is None
    assert db is None
    # 构造过一次且 connect 确实被调用过（失败发生在 connect 阶段）
    assert len(fake_db_cls.instances) == 1
    assert fake_db_cls.instances[0].connect_calls == 1


# ══════════════════════════════════════════════════════════════════
# _maybe_close_sink
# ══════════════════════════════════════════════════════════════════

def test_close_sink_none_is_noop():
    """db_manager=None → 直接返回，不做任何事、不抛。"""
    asyncio.run(_maybe_close_sink(None))  # 实质断言：不抛即通过


def test_close_sink_disconnects():
    """非 None db_manager → disconnect 恰好一次。"""
    db = FakeDatabaseManager(host="x")
    asyncio.run(_maybe_close_sink(db))
    assert db.disconnect_calls == 1


def test_close_sink_disconnect_error_swallowed():
    """disconnect 抛异常 → 吞掉打 warning，不向上抛（finally 语义）。"""
    db = FakeDatabaseManager(host="x")
    db.disconnect_error = RuntimeError("disconnect boom")
    asyncio.run(_maybe_close_sink(db))  # 不应抛
    assert db.disconnect_calls == 1
