"""P0-B / P0-C 修复验收测试：kline-service 表存在性口径统一 + 三态错误契约。

对应验收标准：
- P0-B（存在性口径统一）：
    B1 `table_exists` 必须用 `to_regclass(:table_name) IS NOT NULL`（尊重 search_path）
       且表名以绑定参数传入；
    B2 NULL（不存在）→ False，非空 → True；
    B3 不得再出现 `information_schema` / 硬编码 `table_schema`；
    B4 查询异常不吞（原样上抛，由调用方 fail-closed）；
    B5 读路径（routes）、采集器（collector）复用同一个助手（不再各自内联 SQL）。
- P0-C（去文案匹配、显式错误类型）：
    C1 格式非法 → TableNameFormatError（路由层 400）；
    C2 未采集（表不存在/开关关闭）→ SymbolNotCollectedError（200 空）；
    C3 应采集却不可用（DB 异常/建表失败）→ TableUnavailableError（503 + 告警）。

全部使用假连接/替身，不连接真实数据库与交易所。
"""

import pytest
from fastapi import HTTPException

from api import routes
from core import collector as collector_module
from core.table_name_guard import (
    SymbolNotCollectedError,
    TableNameFormatError,
    TableUnavailableError,
    build_readable_table_name,
)
from shared.utils.table_exists import table_exists

# 复用同目录 R01 验收测试的替身，避免重复实现（DRY）
from test_kline_table_name_guard_r01 import (  # noqa: E402
    _FakeConn,
    _FakeDB,
    _kline_row,
)
from test_kline_read_path_p0_2 import _ExistenceRecorder  # noqa: E402


class _RecordingConn:
    """记录 fetch_val 的 SQL 与参数，并按配置返回结果。"""

    def __init__(self, result=True, error: Exception = None):
        self.result = result
        self.error = error
        self.calls = []

    async def fetch_val(self, query, params=None):
        self.calls.append((query, params))
        if self.error is not None:
            raise self.error
        return self.result


# ============================================================
# P0-B：存在性助手（to_regclass 口径）
# ============================================================

async def test_p0_b_helper_uses_to_regclass_with_bound_param():
    """B1：SQL 为 to_regclass(:table_name) IS NOT NULL，表名走绑定参数。"""
    conn = _RecordingConn(result=True)
    assert await table_exists(conn, "kline_btcusdt_1h") is True
    query, params = conn.calls[0]
    assert "to_regclass" in query
    assert ":table_name" in query
    assert params == {"table_name": "kline_btcusdt_1h"}


async def test_p0_b_helper_returns_false_on_null():
    """B2：不存在（NULL/None）→ False；非空 → True（bool 归一）。"""
    assert await table_exists(_RecordingConn(result=None), "kline_x_1h") is False
    assert await table_exists(_RecordingConn(result=False), "kline_x_1h") is False
    assert await table_exists(_RecordingConn(result=1), "kline_x_1h") is True


async def test_p0_b_helper_no_schema_hardcode():
    """B3：不得再依赖 information_schema / 硬编码 table_schema。"""
    conn = _RecordingConn(result=True)
    await table_exists(conn, "kline_btcusdt_1h")
    query = conn.calls[0][0].lower()
    assert "information_schema" not in query
    assert "table_schema" not in query


async def test_p0_b_helper_propagates_exception():
    """B4：查询异常原样上抛（调用方 fail-closed 处理）。"""
    conn = _RecordingConn(error=RuntimeError("DB 不可用"))
    with pytest.raises(RuntimeError):
        await table_exists(conn, "kline_btcusdt_1h")


async def test_p0_b_routes_and_collector_reuse_same_helper():
    """B5：读路径与采集器引用同一个助手实例（杜绝各自内联）。"""
    assert collector_module.table_exists is table_exists


async def test_p0_b_routes_delegate_uses_to_regclass():
    """B5：routes._table_exists 委派助手，SQL 口径一致。"""
    conn = _RecordingConn(result=True)
    assert await routes._table_exists(conn, "kline_btcusdt_1h") is True
    assert "to_regclass" in conn.calls[0][0]
    assert conn.calls[0][1] == {"table_name": "kline_btcusdt_1h"}


# ============================================================
# P0-C：三态错误类型（按类型区分，不再匹配文案）
# ============================================================

async def test_p0_c_format_error_is_table_name_format_type(real_settings):
    """C1：格式非法 → TableNameFormatError（子类精确类型）。"""
    with pytest.raises(TableNameFormatError):
        await build_readable_table_name(
            "BTCUSDT';DROP TABLE x;--", "1h",
            table_exists=_ExistenceRecorder(result=True), registry=None, settings=real_settings,
        )


async def test_p0_c_missing_table_is_symbol_not_collected_type(real_settings):
    """C2：格式合法但表不存在 → SymbolNotCollectedError。"""
    with pytest.raises(SymbolNotCollectedError):
        await build_readable_table_name(
            "ZZZZUSDT", "1h",
            table_exists=_ExistenceRecorder(result=False), registry=None, settings=real_settings,
        )


async def test_p0_c_switch_off_is_symbol_not_collected_type(real_settings):
    """C2：开关关闭 → SymbolNotCollectedError（未采集口径）。"""
    off = real_settings.model_copy(update={"ALLOW_EXISTING_TABLE_SYMBOLS": False})
    with pytest.raises(SymbolNotCollectedError):
        await build_readable_table_name(
            "USDBRLUSDT", "1h",
            table_exists=_ExistenceRecorder(result=True), registry=None, settings=off,
        )


async def test_p0_c_existence_error_is_table_unavailable_type(real_settings):
    """C3：存在性查询异常 → TableUnavailableError（应采集却不可用的精确类型）。"""
    with pytest.raises(TableUnavailableError):
        await build_readable_table_name(
            "USDBRLUSDT", "1h",
            table_exists=_ExistenceRecorder(error=RuntimeError("DB 不可用")),
            registry=None, settings=real_settings,
        )


# ============================================================
# P0-C：端到端状态码映射（400 / 200 空 / 503 三态互不混淆）
# ============================================================

@pytest.fixture(autouse=True)
def _reset_routes_globals():
    """重置 routes 全局对象与缓存，避免用例间串扰。"""
    old_db, old_collector = routes.db, routes.collector
    routes.db, routes.collector = None, None
    routes._table_exists_cache.clear()
    yield
    routes.db, routes.collector = old_db, old_collector
    routes._table_exists_cache.clear()


async def test_p0_c_route_returns_empty_200_when_not_collected():
    """C2：非白名单且表不存在 → 200 空数据（不告警、不执行 SELECT）。"""
    conn = _FakeConn(table_exists=False)
    routes.db = _FakeDB(conn)
    result = await routes.get_latest_klines(symbol="ZZZZUSDT", interval="1h", limit=10)
    assert result["code"] == 0
    assert result["data"] == []
    assert conn.fetch_all_calls == []


async def test_p0_c_route_returns_503_when_table_unavailable():
    """C3：白名单标的但表不可用（无采集器）→ 503。"""
    conn = _FakeConn(table_exists=False)
    routes.db = _FakeDB(conn)
    with pytest.raises(HTTPException) as exc:
        await routes.get_latest_klines(symbol="BTCUSDT", interval="1h", limit=10)
    assert exc.value.status_code == 503
    assert conn.fetch_all_calls == []


async def test_p0_c_route_returns_400_on_format_error_without_db():
    """C1：格式注入 → 400，且不建立数据库连接。"""
    db = _FakeDB(_FakeConn(table_exists=True))
    routes.db = db
    with pytest.raises(HTTPException) as exc:
        await routes.get_latest_klines(symbol="BTCUSDT';DROP TABLE x;--", interval="1h", limit=10)
    assert exc.value.status_code == 400
    assert db.connection_count == 0
