"""P0-B / P0-C 覆盖率补齐用例（补充既有 P0-B/C 验收测试未覆盖的分支）。

覆盖补充点（均为既有源码真实分支，不改动被测源码）：
- `_validated_read_table_name` 的 TableNameFormatError → 400 分支（直接调用，绕过前置校验）；
- `_ensure_table_ready` 自动建表成功路径（采集器可用且建表成功）；
- 端点既有兜底分支：db 未初始化 → 500、空结果 → 200 空数据、通用异常 → 500、
  以及「表不存在（竞态）」→ 200 空数据的防御分支（/klines/latest 与 /indicators 各覆盖）。

全部使用假连接/替身，不连接真实数据库。
"""
import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock

from api import routes
from core.table_name_guard import TableNameFormatError

# 复用同目录既有测试的替身与常量（DRY）
from test_kline_table_name_guard_r01 import _FakeConn, _FakeDB, _kline_row  # noqa: E402


class _RaisingFetchAllConn(_FakeConn):
    """fetch_all 即抛异常的假连接（模拟查询期 DB 错误/竞态）。"""

    def __init__(self, table_exists: bool, error: Exception):
        super().__init__(table_exists)
        self._error = error

    async def fetch_all(self, query, params=None):
        raise self._error


@pytest.fixture(autouse=True)
def _reset_routes_globals():
    """重置 routes 全局对象与缓存，避免用例间串扰。"""
    old_db, old_collector = routes.db, routes.collector
    routes.db, routes.collector = None, None
    routes._table_exists_cache.clear()
    yield
    routes.db, routes.collector = old_db, old_collector
    routes._table_exists_cache.clear()


# ============================================================
# 契约分支：格式错误直接映射 400（不依赖前置校验）
# ============================================================

async def test_extra_validated_read_table_name_format_error_maps_400():
    """格式非法在 `_validated_read_table_name` 内即转 400，且不执行 SELECT。"""
    conn = _FakeConn(table_exists=True)
    with pytest.raises(HTTPException) as exc:
        await routes._validated_read_table_name(conn, "BTCUSDT';DROP TABLE x;--", "1h")
    assert exc.value.status_code == 400
    assert conn.fetch_all_calls == []


async def test_extra_ensure_table_ready_autocreate_success():
    """表不存在但采集器可用且建表成功 → 正常返回（不抛异常，不执行 SELECT）。"""
    conn = _FakeConn(table_exists=False)
    collector = MagicMock()
    collector.ensure_table = AsyncMock()
    routes.collector = collector

    await routes._ensure_table_ready(conn, "kline_btcusdt_1h", "BTCUSDT", "1h")

    collector.ensure_table.assert_awaited_once_with("BTCUSDT", "1h")
    assert conn.fetch_all_calls == []


# ============================================================
# 端点既有兜底分支：500 / 200 空 / 竞态兜底
# ============================================================

async def test_extra_latest_db_none_returns_500():
    """db 未初始化 → 500。"""
    routes.db = None
    with pytest.raises(HTTPException) as exc:
        await routes.get_latest_klines(symbol="BTCUSDT", interval="1h", limit=10)
    assert exc.value.status_code == 500


async def test_extra_latest_empty_rows_returns_no_data():
    """表可用但查询无行 → 200 空数据。"""
    conn = _FakeConn(table_exists=True, rows=[])
    routes.db = _FakeDB(conn)
    result = await routes.get_latest_klines(symbol="BTCUSDT", interval="1h", limit=10)
    assert result == {"code": 0, "message": "无数据", "data": []}


async def test_extra_latest_generic_exception_returns_500():
    """非「表不存在」的查询异常 → 500（透出错误信息）。"""
    conn = _RaisingFetchAllConn(table_exists=True, error=RuntimeError("连接中断"))
    routes.db = _FakeDB(conn)
    with pytest.raises(HTTPException) as exc:
        await routes.get_latest_klines(symbol="BTCUSDT", interval="1h", limit=10)
    assert exc.value.status_code == 500


async def test_extra_latest_race_missing_table_returns_empty():
    """竞态下表刚被删除（does not exist）→ 兜底 200 空数据。"""
    conn = _RaisingFetchAllConn(
        table_exists=True, error=Exception('relation "kline_btcusdt_1h" does not exist')
    )
    routes.db = _FakeDB(conn)
    result = await routes.get_latest_klines(symbol="BTCUSDT", interval="1h", limit=10)
    assert result == {"code": 0, "message": "无数据", "data": []}


async def test_extra_indicators_db_none_returns_500():
    """db 未初始化 → /indicators 500。"""
    routes.db = None
    with pytest.raises(HTTPException) as exc:
        await routes.get_indicators(symbol="BTCUSDT", interval="1h", period=20)
    assert exc.value.status_code == 500


async def test_extra_indicators_empty_rows_returns_none():
    """表可用但查询无行 → /indicators 200 空（data=None）。"""
    conn = _FakeConn(table_exists=True, rows=[])
    routes.db = _FakeDB(conn)
    result = await routes.get_indicators(symbol="BTCUSDT", interval="1h", period=20)
    assert result == {"code": 0, "message": "无数据", "data": None}


async def test_extra_indicators_not_collected_returns_none():
    """非白名单且表不存在 → /indicators 200 空（data=None，不执行 SELECT）。"""
    conn = _FakeConn(table_exists=False)
    routes.db = _FakeDB(conn)
    result = await routes.get_indicators(symbol="ZZZZUSDT", interval="1h", period=20)
    assert result == {"code": 0, "message": "无数据", "data": None}
    assert conn.fetch_all_calls == []


async def test_extra_indicators_race_missing_table_returns_none():
    """竞态下表刚被删除（does not exist）→ /indicators 兜底 200 空（data=None）。"""
    conn = _RaisingFetchAllConn(
        table_exists=True, error=Exception('relation "kline_btcusdt_1h" does not exist')
    )
    routes.db = _FakeDB(conn)
    result = await routes.get_indicators(symbol="BTCUSDT", interval="1h", period=20)
    assert result == {"code": 0, "message": "无数据", "data": None}


async def test_extra_indicators_generic_exception_returns_500():
    """非「表不存在」的查询异常 → /indicators 500。"""
    conn = _RaisingFetchAllConn(table_exists=True, error=RuntimeError("连接中断"))
    routes.db = _FakeDB(conn)
    with pytest.raises(HTTPException) as exc:
        await routes.get_indicators(symbol="BTCUSDT", interval="1h", period=20)
    assert exc.value.status_code == 500
