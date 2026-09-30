"""R01 修复验收测试：K 线表名校验与 SQL 结构注入防护。

覆盖验收标准：
- R01-AC1/AC2：非法 symbol/interval → HTTP 4xx，未触达任何 SQL
- R01-AC3：合法参数但表不存在且自动建表失败 → 返回「无数据」，不执行 SELECT
- R01-AC4：合法请求行为与修复前一致（正常返回数据）
- R01-AC5：/indicators、/collect/manual 拒绝行为与 /klines/latest 一致
- R01-AC6：表名正则单元测试（合法通过 / 注入拒绝）

说明：全部使用假连接与假采集器，不连接真实数据库与交易所。
"""

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from api import routes
from core.table_name_guard import (
    TableNameValidationError,
    build_kline_table_name,
    is_valid_table_name,
    validate_symbol_interval_format,
)


# ============================================================
# 测试替身：假连接 / 假数据库 / 假采集器
# ============================================================

class _FakeConn:
    """假数据库连接：可配置表是否存在与返回行，并记录 fetch_all 调用。"""

    def __init__(self, table_exists: bool, rows=None):
        self._table_exists = table_exists
        self._rows = rows or []
        self.fetch_all_calls = []

    async def fetch_val(self, query, params=None):
        return self._table_exists

    async def fetch_all(self, query, params=None):
        self.fetch_all_calls.append((query, params))
        return self._rows


class _FakeConnCtx:
    """db.get_connection() 的异步上下文管理器替身。"""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


class _FakeDB:
    """假数据库：统计 get_connection 调用次数以断言是否触达 SQL。"""

    def __init__(self, conn):
        self._conn = conn
        self.connection_count = 0

    def get_connection(self):
        self.connection_count += 1
        return _FakeConnCtx(self._conn)


def _kline_row(second: int = 0) -> dict:
    """构造一行 K 线（字段与真实表列一致）。"""
    base = datetime(2024, 1, 1, 0, 0, 0)
    return {
        "open_time": base + timedelta(seconds=second),
        "open_price": "100.0",
        "high_price": "110.0",
        "low_price": "90.0",
        "close_price": "105.0",
        "volume": "12.0",
        "close_time": base + timedelta(seconds=second, hours=1),
        "quote_volume": "1000.0",
        "trade_count": 5,
        "taker_buy_volume": "6.0",
        "taker_buy_quote_volume": "500.0",
    }


@pytest.fixture(autouse=True)
def _reset_routes_globals():
    """每个用例前重置 routes 全局对象，避免用例间串扰。"""
    old_db, old_collector = routes.db, routes.collector
    routes.db, routes.collector = None, None
    yield
    routes.db, routes.collector = old_db, old_collector


# ============================================================
# R01-AC1 / R01-AC2：非法输入拒绝且不触达 SQL
# ============================================================

async def test_r01_ac1_symbol_injection_rejected_without_sql():
    """symbol 注入（DROP TABLE）→ 400，且未建立数据库连接。"""
    db = _FakeDB(_FakeConn(table_exists=True))
    routes.db = db
    with pytest.raises(HTTPException) as exc:
        await routes.get_latest_klines(symbol="BTCUSDT';DROP TABLE x;--", interval="1h", limit=10)
    assert exc.value.status_code == 400
    assert db.connection_count == 0


async def test_r01_ac2_interval_injection_rejected_without_sql():
    """interval 注入（CROSS JOIN）→ 400，且未触达 fetch_all。"""
    conn = _FakeConn(table_exists=True)
    db = _FakeDB(conn)
    routes.db = db
    with pytest.raises(HTTPException) as exc:
        await routes.get_latest_klines(symbol="BTCUSDT", interval="1h CROSS JOIN x", limit=10)
    assert exc.value.status_code == 400
    assert db.connection_count == 0
    assert conn.fetch_all_calls == []


# ============================================================
# R01-AC3：建表失败即终止，不执行 SELECT
# ============================================================

async def test_r01_ac3_ensure_table_failure_returns_no_data_without_select():
    """表不存在且自动建表抛异常 → 返回「无数据」，且不执行 SELECT。"""
    conn = _FakeConn(table_exists=False)
    routes.db = _FakeDB(conn)

    class _BrokenCollector:
        async def ensure_table(self, symbol, interval):
            raise RuntimeError("建表失败")

    routes.collector = _BrokenCollector()
    result = await routes.get_latest_klines(symbol="BTCUSDT", interval="1h", limit=10)
    assert result["data"] == []
    assert result["message"] == "无数据"
    assert conn.fetch_all_calls == []


async def test_r01_ac3_collector_unavailable_returns_no_data():
    """表不存在且采集器不可用 → 返回「无数据」，且不执行 SELECT。"""
    conn = _FakeConn(table_exists=False)
    routes.db = _FakeDB(conn)
    routes.collector = None
    result = await routes.get_latest_klines(symbol="BTCUSDT", interval="1h", limit=10)
    assert result["data"] == []
    assert conn.fetch_all_calls == []


# ============================================================
# R01-AC4：合法请求行为不回归
# ============================================================

async def test_r01_ac4_valid_request_returns_data():
    """合法 BTCUSDT/1h 正常返回数据（无回归）。"""
    conn = _FakeConn(table_exists=True, rows=[_kline_row(0), _kline_row(3600)])
    db = _FakeDB(conn)
    routes.db = db
    result = await routes.get_latest_klines(symbol="BTCUSDT", interval="1h", limit=10)
    assert result["code"] == 0
    assert result["message"] == "success"
    assert len(result["data"]) == 2
    assert result["data"][0]["symbol"] == "BTCUSDT"
    assert conn.fetch_all_calls[0][0].startswith("\n                SELECT * FROM kline_btcusdt_1h")


# ============================================================
# R01-AC5：/indicators 与 /collect/manual 拒绝行为一致
# ============================================================

async def test_r01_ac5_indicators_rejects_invalid_interval():
    """/indicators 对注入 interval → 400，且未触达 SQL。"""
    db = _FakeDB(_FakeConn(table_exists=True))
    routes.db = db
    with pytest.raises(HTTPException) as exc:
        await routes.get_indicators(symbol="BTCUSDT", interval="1h CROSS JOIN x", period=100)
    assert exc.value.status_code == 400
    assert db.connection_count == 0


async def test_r01_ac5_manual_collect_rejects_invalid_symbol():
    """/collect/manual 对注入 symbol → 400，且未调用采集。"""
    class _SpyCollector:
        def __init__(self):
            self.called = False

        async def collect_recent(self, symbol, interval, minutes):
            self.called = True
            return 0

    spy = _SpyCollector()
    routes.collector = spy
    with pytest.raises(HTTPException) as exc:
        await routes.manual_collect(symbol="BTCUSDT';DROP TABLE x;--", interval="1h", minutes=5)
    assert exc.value.status_code == 400
    assert spy.called is False


# ============================================================
# R01-AC6：表名正则单元测试
# ============================================================

@pytest.mark.parametrize(
    "table_name",
    ["kline_btcusdt_1h", "kline_ethusdt_15m", "kline_solusdt_1d"],
)
def test_r01_ac6_valid_table_name_accepted(table_name, real_settings):
    """合法表名匹配真实默认正则。"""
    assert is_valid_table_name(table_name, settings=real_settings) is True


@pytest.mark.parametrize(
    "table_name",
    [
        "kline_btcusdt_1h; DROP TABLE users;--",
        "kline_btcusdt_1h CROSS JOIN x",
        "kline_btcusdt_1h'",
        "kline_btcusdt_1h ",
        "kline_btcusdt_1H",
        "KLINE_BTCUSDT_1H",
        "kline_btcusdt_1h\n",
        "kline_btc-eth_1h",
        "",
    ],
)
def test_r01_ac6_injected_table_name_rejected(table_name, real_settings):
    """注入样例/含空格/分号/引号/CROSS JOIN/大写可疑片段全部拒绝。"""
    assert is_valid_table_name(table_name, settings=real_settings) is False


# ============================================================
# R01-F1/F2：build_kline_table_name 三层校验
# ============================================================

def test_build_table_name_normalizes_symbol_case(real_settings):
    """symbol 统一大写比对，表名小写生成。"""
    assert build_kline_table_name("btcusdt", "1h", settings=real_settings) == "kline_btcusdt_1h"


def test_build_table_name_rejects_symbol_injection(real_settings):
    with pytest.raises(TableNameValidationError):
        build_kline_table_name("BTCUSDT';DROP TABLE x;--", "1h", settings=real_settings)


def test_build_table_name_rejects_interval_injection(real_settings):
    with pytest.raises(TableNameValidationError):
        build_kline_table_name("BTCUSDT", "1h CROSS JOIN x", settings=real_settings)


def test_build_table_name_rejects_symbol_not_in_whitelist(real_settings):
    """格式合法但不在白名单的 symbol 必须拒绝（fail-closed）。"""
    with pytest.raises(TableNameValidationError):
        build_kline_table_name("FOOUSDT", "1h", settings=real_settings)


def test_build_table_name_rejects_interval_not_in_whitelist(real_settings):
    with pytest.raises(TableNameValidationError):
        build_kline_table_name("BTCUSDT", "3m", settings=real_settings)


def test_build_table_name_uses_registry_active_symbols(real_settings):
    """白名单需实时反映 SymbolRegistry 已激活标的（非启动快照）。"""

    class _Cfg:
        symbol = "DOGEUSDT"
        intervals = ["5m"]

    class _Reg:
        def get_active_symbols(self):
            return [_Cfg()]

    assert (
        build_kline_table_name("dogeusdt", "5m", registry=_Reg(), settings=real_settings)
        == "kline_dogeusdt_5m"
    )


def test_build_table_name_registry_failure_is_fail_closed(real_settings):
    """注册表不可用时降级为静态白名单，仍拒绝白名单外输入。"""

    class _BadReg:
        def get_active_symbols(self):
            raise RuntimeError("数据库不可用")

    # 静态白名单内仍可用
    assert (
        build_kline_table_name("BTCUSDT", "1h", registry=_BadReg(), settings=real_settings)
        == "kline_btcusdt_1h"
    )
    # 静态白名单外一律拒绝
    with pytest.raises(TableNameValidationError):
        build_kline_table_name("DOGEUSDT", "5m", registry=_BadReg(), settings=real_settings)


def test_is_valid_table_name_fail_closed_without_pattern():
    """未配置 TABLE_NAME_PATTERN 时 fail-closed 返回 False 且拒绝构造。"""

    class _Cfg:
        FIXED_SYMBOLS = "BTCUSDT"
        SYMBOLS = ""
        COLLECT_INTERVALS = "1h"
        SYMBOL_FORMAT_PATTERN = r"^[A-Z0-9]{3,20}$"

    assert is_valid_table_name("kline_btcusdt_1h", settings=_Cfg()) is False
    with pytest.raises(TableNameValidationError):
        build_kline_table_name("BTCUSDT", "1h", settings=_Cfg())


# ============================================================
# R01-F4：format 层校验（供注册入口先行防注入）
# ============================================================

def test_validate_format_normalizes_whitespace(real_settings):
    symbol, interval = validate_symbol_interval_format(" btcusdt ", " 1h ", settings=real_settings)
    assert symbol == "BTCUSDT"
    assert interval == "1h"


def test_validate_format_rejects_non_string(real_settings):
    with pytest.raises(TableNameValidationError):
        validate_symbol_interval_format(123, "1h", settings=real_settings)


def test_validate_format_rejects_blank_interval(real_settings):
    with pytest.raises(TableNameValidationError):
        validate_symbol_interval_format("BTCUSDT", "   ", settings=real_settings)