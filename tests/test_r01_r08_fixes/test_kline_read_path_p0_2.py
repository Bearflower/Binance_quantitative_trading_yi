"""P0-2 修复验收测试：读路径「白名单 OR 表已存在」放行。

对应验收标准（架构 §11）：
- P0-2-AC1：表已存在且非白名单 → 放行并返回数据（现场标的 USDBRLUSDT/ACNUSDT）
- P0-2-AC2：非法 symbol → 4xx，且不建立 DB 连接（存在性回调未被调用）
- P0-2-AC3：非白名单且表不存在 → 4xx，且不执行 SELECT
- P0-2-AC4：白名单命中行为不变（正常返回数据）
- P0-2-AC5：表存在性查询异常 → fail-closed（4xx，不放行）
- P0-2-AC7：/indicators 与 /klines/latest 的读路径行为一致

本文件与同目录 R01 验收测试共用假连接/假数据库替身与真实配置夹具，
避免重复实现（DRY）；全部使用替身，不连接真实数据库与交易所。
"""

import time

import pytest
from fastapi import HTTPException

from api import routes
from core.table_name_guard import (
    TableNameValidationError,
    build_readable_table_name,
    build_table_name_by_format,
)

# 复用 R01 验收测试的测试替身，杜绝拷贝（同目录、无需 __init__.py）
from test_kline_table_name_guard_r01 import (
    _FakeConn,
    _FakeDB,
    _kline_row,
)

# 格式合法但不在白名单的标的：前者「表已存在」（生产 P0 现场），后者「表不存在」
_EXISTING_TABLE_SYMBOL = "USDBRLUSDT"
_NON_WHITELIST_MISSING = "DOGEUSDT"


class _ExistenceRecorder:
    """存在性回调替身：记录调用表名，并按配置返回结果或抛异常。"""

    def __init__(self, result: bool = True, error: Exception = None):
        self.result = result
        self.error = error
        self.calls = []

    async def __call__(self, table_name: str) -> bool:
        self.calls.append(table_name)
        if self.error is not None:
            raise self.error
        return self.result


class _BrokenExistenceConn(_FakeConn):
    """存在性查询即抛异常的假连接（模拟 information_schema 不可用）。"""

    async def fetch_val(self, query, params=None):
        raise RuntimeError("information_schema 不可用")


class _CountingExistenceConn(_FakeConn):
    """记录「表存在性查询」次数的假连接（用于验证 TTL 缓存是否命中）。"""

    def __init__(self, table_exists: bool):
        super().__init__(table_exists)
        self.fetch_val_count = 0

    async def fetch_val(self, query, params=None):
        self.fetch_val_count += 1
        return self._table_exists


@pytest.fixture(autouse=True)
def _reset_routes_globals():
    """每个用例前重置 routes 全局对象，避免用例间串扰。"""
    old_db, old_collector = routes.db, routes.collector
    routes.db, routes.collector = None, None
    yield
    routes.db, routes.collector = old_db, old_collector


@pytest.fixture(autouse=True)
def _clear_table_exists_cache():
    """每个用例前后清空 routes 的模块级存在性缓存，避免跨用例串扰。"""
    routes._table_exists_cache.clear()
    yield
    routes._table_exists_cache.clear()


# ============================================================
# guard 层：build_readable_table_name 的判定分支
# ============================================================

async def test_p0_2_guard_whitelist_hit_skips_existence_check(real_settings):
    """白名单命中 → 直接放行，且不调用存在性回调。"""
    rec = _ExistenceRecorder(result=True)
    name = await build_readable_table_name(
        "BTCUSDT", "1h", table_exists=rec, registry=None, settings=real_settings
    )
    assert name == "kline_btcusdt_1h"
    assert rec.calls == []


async def test_p0_2_guard_existing_table_allows_non_whitelisted(real_settings):
    """非白名单 + 表已存在 → 放行（source=existing_table）。"""
    rec = _ExistenceRecorder(result=True)
    name = await build_readable_table_name(
        _EXISTING_TABLE_SYMBOL, "1h", table_exists=rec, registry=None, settings=real_settings
    )
    assert name == "kline_usdbrlusdt_1h"
    assert rec.calls == ["kline_usdbrlusdt_1h"]


async def test_p0_2_guard_missing_table_rejected(real_settings):
    """非白名单 + 表不存在 → 拒绝。"""
    rec = _ExistenceRecorder(result=False)
    with pytest.raises(TableNameValidationError):
        await build_readable_table_name(
            _NON_WHITELIST_MISSING, "1h", table_exists=rec, registry=None, settings=real_settings
        )
    assert rec.calls == ["kline_dogeusdt_1h"]


async def test_p0_2_guard_existence_error_fail_closed(real_settings):
    """存在性回调抛异常 → 拒绝（fail-closed），绝不放行。"""
    rec = _ExistenceRecorder(error=RuntimeError("DB 不可用"))
    with pytest.raises(TableNameValidationError):
        await build_readable_table_name(
            _EXISTING_TABLE_SYMBOL, "1h", table_exists=rec, registry=None, settings=real_settings
        )
    assert rec.calls == ["kline_usdbrlusdt_1h"]


async def test_p0_2_guard_switch_off_rejects_without_calling_callback(real_settings):
    """开关关闭 → 即使表存在也拒绝，且不调用存在性回调（不回归旧行为）。"""
    off = real_settings.model_copy(update={"ALLOW_EXISTING_TABLE_SYMBOLS": False})
    rec = _ExistenceRecorder(result=True)
    with pytest.raises(TableNameValidationError):
        await build_readable_table_name(
            _EXISTING_TABLE_SYMBOL, "1h", table_exists=rec, registry=None, settings=off
        )
    assert rec.calls == []


async def test_p0_2_guard_no_callback_rejects(real_settings):
    """未注入存在性回调 → 拒绝（fail-closed）。"""
    with pytest.raises(TableNameValidationError):
        await build_readable_table_name(
            _EXISTING_TABLE_SYMBOL, "1h", table_exists=None, registry=None, settings=real_settings
        )


async def test_p0_2_guard_injection_rejected_without_callback(real_settings):
    """格式层即拒绝注入，且不触达存在性回调。"""
    rec = _ExistenceRecorder(result=True)
    with pytest.raises(TableNameValidationError):
        await build_readable_table_name(
            "BTCUSDT';DROP TABLE x;--", "1h", table_exists=rec, registry=None, settings=real_settings
        )
    assert rec.calls == []


def test_p0_2_format_only_entry_rejects_injection(real_settings):
    """build_table_name_by_format：合法放行、注入拒绝。"""
    assert (
        build_table_name_by_format("BTCUSDT", "1h", settings=real_settings)
        == "kline_btcusdt_1h"
    )
    with pytest.raises(TableNameValidationError):
        build_table_name_by_format("BTCUSDT", "1h CROSS JOIN x", settings=real_settings)


# ============================================================
# 路由层：/klines/latest 的放行与拒绝
# ============================================================

async def test_p0_2_ac1_existing_table_non_whitelist_returns_data():
    """表已存在且非白名单 → 200 且带数据（部署后 ATR 可恢复）。"""
    conn = _FakeConn(table_exists=True, rows=[_kline_row(0), _kline_row(3600)])
    routes.db = _FakeDB(conn)
    result = await routes.get_latest_klines(symbol=_EXISTING_TABLE_SYMBOL, interval="1h", limit=10)
    assert result["code"] == 0
    assert result["message"] == "success"
    assert len(result["data"]) == 2
    assert conn.fetch_all_calls[0][0].startswith(
        "\n                SELECT * FROM kline_usdbrlusdt_1h"
    )


async def test_p0_2_ac2_injection_rejected_without_db():
    """注入 symbol → 400，且未建立数据库连接。"""
    db = _FakeDB(_FakeConn(table_exists=True))
    routes.db = db
    with pytest.raises(HTTPException) as exc:
        await routes.get_latest_klines(symbol="BTCUSDT';DROP TABLE x;--", interval="1h", limit=10)
    assert exc.value.status_code == 400
    assert db.connection_count == 0


async def test_p0_2_ac3_non_whitelist_missing_table_rejected_without_select():
    """非白名单且表不存在 → 400，且不执行 SELECT。"""
    conn = _FakeConn(table_exists=False)
    routes.db = _FakeDB(conn)
    with pytest.raises(HTTPException) as exc:
        await routes.get_latest_klines(symbol=_NON_WHITELIST_MISSING, interval="1h", limit=10)
    assert exc.value.status_code == 400
    assert conn.fetch_all_calls == []


async def test_p0_2_ac4_whitelist_behavior_unchanged():
    """白名单命中行为与修复前一致（原逻辑不变）。"""
    conn = _FakeConn(table_exists=True, rows=[_kline_row(0)])
    routes.db = _FakeDB(conn)
    result = await routes.get_latest_klines(symbol="BTCUSDT", interval="1h", limit=10)
    assert result["code"] == 0
    assert result["data"][0]["symbol"] == "BTCUSDT"
    assert conn.fetch_all_calls[0][0].startswith("\n                SELECT * FROM kline_btcusdt_1h")


async def test_p0_2_ac5_existence_query_error_fail_closed():
    """存在性查询异常 → 400（不放行），不执行 SELECT。"""
    conn = _BrokenExistenceConn(table_exists=True)
    routes.db = _FakeDB(conn)
    with pytest.raises(HTTPException) as exc:
        await routes.get_latest_klines(symbol=_EXISTING_TABLE_SYMBOL, interval="1h", limit=10)
    assert exc.value.status_code == 400
    assert conn.fetch_all_calls == []


# ============================================================
# P0-2-AC7：/indicators 与 /klines/latest 读路径行为一致
# ============================================================

async def test_p0_2_ac7_indicators_allows_existing_table_symbol():
    """/indicators 对「表已存在」的非白名单标的同样放行并触达 SELECT。"""
    conn = _FakeConn(table_exists=True, rows=[_kline_row(0), _kline_row(3600)])
    routes.db = _FakeDB(conn)
    result = await routes.get_indicators(symbol=_EXISTING_TABLE_SYMBOL, interval="1h", period=20)
    assert result["code"] == 0
    assert conn.fetch_all_calls  # 已通过 guard 并执行 SELECT（与 /klines/latest 一致）


async def test_p0_2_ac7_indicators_rejects_injection_without_db():
    """/indicators 注入 symbol → 400，且未建立数据库连接。"""
    db = _FakeDB(_FakeConn(table_exists=True))
    routes.db = db
    with pytest.raises(HTTPException) as exc:
        await routes.get_indicators(symbol="BTCUSDT';DROP TABLE x;--", interval="1h", period=20)
    assert exc.value.status_code == 400
    assert db.connection_count == 0


# ============================================================
# routes 层：表存在性 TTL 缓存（EXISTING_TABLE_CHECK_CACHE_TTL_SECONDS）
# ============================================================

_TABLE = "kline_dogeusdt_1h"


def test_read_table_exists_cache_ttl_reads_configured_value(real_settings, monkeypatch):
    """_read_table_exists_cache_ttl 直接读取配置项（>0 时原样返回）。"""
    monkeypatch.setattr(real_settings, "EXISTING_TABLE_CHECK_CACHE_TTL_SECONDS", 25)
    assert routes._read_table_exists_cache_ttl() == 25


def test_cache_lookup_hit_within_ttl():
    """未过期的缓存条目 → 返回缓存存在性（不查库）。"""
    routes._table_exists_cache[_TABLE] = (True, time.monotonic())
    assert routes._cache_lookup(_TABLE, 60) is True


def test_cache_lookup_expired_returns_none():
    """已过期的缓存条目 → 返回 None（触发重新查库）。"""
    routes._table_exists_cache[_TABLE] = (True, time.monotonic() - 61)
    assert routes._cache_lookup(_TABLE, 60) is None


@pytest.mark.parametrize("ttl", [0, -1])
def test_cache_lookup_non_positive_ttl_returns_none(ttl):
    """ttl <= 0 → 一律视为未命中（实时查询，不读缓存）。"""
    routes._table_exists_cache[_TABLE] = (True, time.monotonic())
    assert routes._cache_lookup(_TABLE, ttl) is None


async def test_resolve_table_exists_caches_within_ttl(monkeypatch):
    """TTL > 0：同一表名第二次查询命中缓存，不再触达 DB。"""
    monkeypatch.setattr(routes, "_read_table_exists_cache_ttl", lambda: 60)
    conn = _CountingExistenceConn(table_exists=True)

    assert await routes._resolve_table_exists(conn, _TABLE) is True
    assert await routes._resolve_table_exists(conn, _TABLE) is True
    assert conn.fetch_val_count == 1  # 第二次命中缓存，仅查库一次


async def test_resolve_table_exists_requeries_after_expiry(monkeypatch):
    """TTL > 0 且缓存已过期：重新查库并覆盖缓存。"""
    monkeypatch.setattr(routes, "_read_table_exists_cache_ttl", lambda: 60)
    conn = _CountingExistenceConn(table_exists=True)

    await routes._resolve_table_exists(conn, _TABLE)
    # 回拨缓存时间戳到 TTL 之前，模拟过期（确定性，不依赖真实等待）
    exists, ts = routes._table_exists_cache[_TABLE]
    routes._table_exists_cache[_TABLE] = (exists, ts - 61)
    await routes._resolve_table_exists(conn, _TABLE)

    assert conn.fetch_val_count == 2


async def test_resolve_table_exists_ttl_zero_queries_every_time(monkeypatch):
    """TTL = 0：每次调用都查库，且不写入缓存。"""
    monkeypatch.setattr(routes, "_read_table_exists_cache_ttl", lambda: 0)
    conn = _CountingExistenceConn(table_exists=True)

    await routes._resolve_table_exists(conn, _TABLE)
    await routes._resolve_table_exists(conn, _TABLE)

    assert conn.fetch_val_count == 2
    assert routes._table_exists_cache == {}


# ============================================================
# routes 层：读路径共用入口 _resolve_ready_table（消除两端点同构）
# ============================================================

async def test_resolve_ready_table_returns_name_when_ready():
    """表可用（guard 通过 + 表就绪）→ 返回表名。"""
    conn = _FakeConn(table_exists=True)
    assert await routes._resolve_ready_table(conn, "BTCUSDT", "1h") == "kline_btcusdt_1h"


async def test_resolve_ready_table_returns_empty_when_not_ready():
    """白名单标的但表不可用（不存在且无采集器）→ 返回空字符串。"""
    conn = _FakeConn(table_exists=False)
    assert await routes._resolve_ready_table(conn, "BTCUSDT", "1h") == ""


async def test_p0_2_latest_no_data_when_table_unavailable():
    """表不可用 → /klines/latest 返回「无数据」（data=[]）且不执行 SELECT。"""
    conn = _FakeConn(table_exists=False)
    routes.db = _FakeDB(conn)
    result = await routes.get_latest_klines(symbol="BTCUSDT", interval="1h", limit=10)
    assert result == {"code": 0, "message": "无数据", "data": []}
    assert conn.fetch_all_calls == []


async def test_p0_2_indicators_no_data_when_table_unavailable():
    """表不可用 → /indicators 返回「无数据」（data=None），返回体与 latest 不同。"""
    conn = _FakeConn(table_exists=False)
    routes.db = _FakeDB(conn)
    result = await routes.get_indicators(symbol="BTCUSDT", interval="1h", period=20)
    assert result == {"code": 0, "message": "无数据", "data": None}
    assert conn.fetch_all_calls == []
