# -*- coding: utf-8 -*-
"""
MTPCS 信号持久化模块测试

覆盖：
  1. build_dedup_key 格式与大小写归一
  2. ensure_signal_table_exists 的 DDL 调用
  3. persist_signal executed 成功落库：SQL 文本 + 参数 + status='executed'
  4. persist_signal 重复键 ON CONFLICT 升级条件存在
  5. persist_signal 写库抛异常 → 返回 False、不 raise、触发 notification.send_alert
  6. persist_signal cancelled 信号写入并带 skip_reason/reject_reason
  7. schema 名安全检查（非法字符 / 非 str 输入）
  8. 决策时间解析：timestamp 兜底 created_at 缺失场景
  9. metadata 值的嵌套 dict/list 与 Decimal 序列化
  10. _json_default 的异常兜底分支
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.mtpc_signal_persistence import (
    BEIJING_TZ,
    _ALTER_ADD_DEDUP_KEY,
    _CREATE_UNIQUE_INDEX_DEDUP,
    _INSERT_SIGNAL_SQL,
    _json_default,
    _resolve_decision_time,
    _sanitize_schema,
    build_dedup_key,
    ensure_signal_table_exists,
    persist_signal,
)


# ===================================================================
# 公共 fixture：FakeDB（用于 execute_ddl / execute，记录调用）
# ===================================================================


class FakeDB:
    """DatabaseManager 测试桩：记录调用的 SQL + args，支持注入异常"""

    def __init__(self, *, execute_raises=None, execute_ddl_raises=None):
        self.execute_calls = []  # [(sql, args_tuple), ...]
        self.execute_ddl_calls = []
        self.execute_raises = execute_raises
        self.execute_ddl_raises = execute_ddl_raises

    async def execute(self, sql, *args, **kwargs):
        self.execute_calls.append((sql, args))
        if self.execute_raises is not None:
            raise self.execute_raises
        return "INSERT 0 1"

    async def execute_ddl(self, sql, *args, **kwargs):
        self.execute_ddl_calls.append(sql)
        if self.execute_ddl_raises is not None:
            raise self.execute_ddl_raises
        return "ALTER TABLE"


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.fixture
def alert_client():
    """NotificationClient 测试桩：send_alert 是 AsyncMock"""
    nc = MagicMock()
    nc.send_alert = AsyncMock(return_value=True)
    return nc


# ===================================================================
# 1. build_dedup_key 格式与大小写归一
# ===================================================================


class TestBuildDedupKey:
    def test_format_and_case(self):
        """SYMBOL:DIRECTION:YYYYMMDDHH 格式，symbol/direction 统一大写"""
        created = datetime(2026, 10, 19, 14, 37, 55)
        key = build_dedup_key("btcusdt", "long", created)
        assert key == "BTCUSDT:LONG:2026101914"

    def test_mixed_case(self):
        """大小写归一化"""
        created = datetime(2026, 10, 19, 9, 0, 0)
        key = build_dedup_key("EthUsdt", "Short", created)
        assert key == "ETHUSDT:SHORT:2026101909"

    def test_falsy_values_go_blank(self):
        """空字符串归一为空（不是空格），key 形态为 ::HHHHMMDDHH"""
        created = datetime(2026, 10, 19, 9, 0, 0)
        key = build_dedup_key("", "", created)
        assert key == "::2026101909"

    def test_invalid_created_at_type_falls_back_to_now(self):
        """created_at 不是 datetime 对象时回退到当前北京时间"""
        key = build_dedup_key("BTCUSDT", "LONG", "not-a-datetime")
        # 小时部分应该匹配当前北京小时（允许 ±1 秒内）
        now_hour = datetime.now(BEIJING_TZ).strftime("%Y%m%d%H")
        assert key.endswith(f":{now_hour}")

    def test_timezone_aware_input_ok(self):
        """带时区的 datetime 也能 strftime"""
        created = datetime(2026, 10, 19, 14, 0, 0, tzinfo=BEIJING_TZ)
        key = build_dedup_key("BTCUSDT", "LONG", created)
        assert key == "BTCUSDT:LONG:2026101914"


# ===================================================================
# 2. ensure_signal_table_exists 的 DDL 调用
# ===================================================================


@pytest.mark.asyncio
async def test_ensure_signal_table_exists_calls_ddl(fake_db: FakeDB):
    """ensure_signal_table_exists 对有效 schema 顺序调用 2 次 execute_ddl"""
    await ensure_signal_table_exists(fake_db, "btc_eth")
    assert len(fake_db.execute_ddl_calls) == 2
    # 第一次是 ALTER TABLE ... ADD COLUMN IF NOT EXISTS dedup_key
    assert "ALTER TABLE btc_eth.trade_signals" in fake_db.execute_ddl_calls[0]
    assert "ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64)" in fake_db.execute_ddl_calls[0]
    # 第二次是 CREATE UNIQUE INDEX IF NOT EXISTS uq_btc_eth_signals_dedup
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_btc_eth_signals_dedup" in fake_db.execute_ddl_calls[1]


@pytest.mark.asyncio
async def test_ensure_signal_table_exists_failure_swallowed(fake_db: FakeDB):
    """execute_ddl 抛异常时 ensure_signal_table_exists 不 raise（静默吞掉）"""
    fake_db.execute_ddl_raises = RuntimeError("连接失败")
    # 不应抛
    await ensure_signal_table_exists(fake_db, "btc_eth")


# ===================================================================
# 3. persist_signal executed 成功落库
# ===================================================================


@pytest.mark.asyncio
async def test_persist_signal_executed_success(fake_db: FakeDB):
    """trade_success=True → status='executed'，SQL 含 ON CONFLICT，参数正确"""
    signal = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "grade": "A",
        "score": 92,
        "entry_price": 65000.0,
        "trade_success": True,
        "created_at": datetime(2026, 10, 19, 14, 0, 0),
        "initial_stop_loss": 64000.0,
        "tp1_price": 67000.0,
    }
    ok = await persist_signal(fake_db, "btc_eth", signal)
    assert ok is True
    assert len(fake_db.execute_calls) == 1

    sql, args = fake_db.execute_calls[0]
    # SQL 结构校验
    assert "INSERT INTO btc_eth.trade_signals" in sql
    assert "ON CONFLICT (dedup_key) DO UPDATE" in sql
    assert "WHERE btc_eth.trade_signals.status = 'cancelled'" in sql
    assert "AND EXCLUDED.status                = 'executed'" in sql

    # 参数顺序：symbol, direction, grade, score, entry_price, dedup_key, status, executed_at, metadata_json
    assert args[0] == "BTCUSDT"
    assert args[1] == "LONG"
    assert args[2] == "A"
    assert args[3] == 92
    assert args[4] == 65000.0
    assert args[5] == "BTCUSDT:LONG:2026101914"  # dedup_key
    assert args[6] == "executed"  # status
    # args[7] 是 naive datetime（executed_at）
    assert isinstance(args[7], datetime)
    assert args[7].tzinfo is None
    # args[8] 是 JSON 字符串（metadata）
    meta = json.loads(args[8])
    assert meta.get("initial_stop_loss") == 64000.0
    assert meta.get("tp1_price") == 67000.0
    assert "trade_success" not in meta  # 主字段不应在 metadata 里
    assert "symbol" not in meta


# ===================================================================
# 4. 重复键 ON CONFLICT 升级条件存在
# ===================================================================


@pytest.mark.asyncio
async def test_on_conflict_upgrade_condition_exists(fake_db: FakeDB):
    """确认 SQL 里只允许 cancelled→executed 升级覆盖"""
    signal = {
        "symbol": "ETHUSDT",
        "direction": "SHORT",
        "grade": "B",
        "score": 75,
        "entry_price": 3200.0,
        "trade_success": True,
        "created_at": datetime(2026, 10, 19, 15, 30, 0),
    }
    await persist_signal(fake_db, "btc_eth_aggressive", signal)
    sql, _ = fake_db.execute_calls[0]
    assert "btc_eth_aggressive.trade_signals" in sql
    # 升级条件：仅 cancelled→executed
    assert "status = 'cancelled'" in sql
    assert "EXCLUDED.status                = 'executed'" in sql


# ===================================================================
# 5. 写库抛异常 → persist_signal 返回 False、不 raise、触发告警
# ===================================================================


@pytest.mark.asyncio
async def test_persist_signal_db_error_swallowed_and_alerts(fake_db: FakeDB, alert_client):
    """db.execute 抛异常 → persist_signal 不 raise，返回 False，
    notification.send_alert 被调用（含标题和 schema 信息）"""
    fake_db.execute_raises = ConnectionError("数据库挂了")

    signal = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "grade": "A",
        "score": 92,
        "entry_price": 65000.0,
        "trade_success": True,
        "created_at": datetime(2026, 10, 19, 14, 0, 0),
    }

    # 不 raise
    ok = await persist_signal(fake_db, "btc_eth", signal, notification=alert_client)
    assert ok is False

    # 告警被调用
    alert_client.send_alert.assert_awaited_once()
    call_kwargs = alert_client.send_alert.call_args.kwargs
    assert call_kwargs["title"] == "信号持久化失败"
    assert "btc_eth" in call_kwargs["message"]
    assert "BTCUSDT" in call_kwargs["message"]
    assert call_kwargs["level"] == "warning"


@pytest.mark.asyncio
async def test_persist_signal_error_no_notification(fake_db: FakeDB):
    """notification=None 时，写库异常也不会 raise，只是静默返回 False"""
    fake_db.execute_raises = ConnectionError("数据库挂了")
    signal = {
        "symbol": "BTCUSDT", "direction": "LONG", "grade": "A",
        "score": 92, "entry_price": 65000.0, "trade_success": True,
        "created_at": datetime(2026, 10, 19, 14, 0, 0),
    }
    ok = await persist_signal(fake_db, "btc_eth", signal, notification=None)
    assert ok is False


@pytest.mark.asyncio
async def test_alert_failure_swallowed(fake_db: FakeDB):
    """send_alert 自身抛异常 → persist_signal 仍不 raise"""
    fake_db.execute_raises = ConnectionError("数据库挂了")
    alert_client = MagicMock()
    alert_client.send_alert = AsyncMock(side_effect=RuntimeError("webhook 挂了"))

    signal = {
        "symbol": "BTCUSDT", "direction": "LONG", "grade": "A",
        "score": 92, "entry_price": 65000.0, "trade_success": True,
        "created_at": datetime(2026, 10, 19, 14, 0, 0),
    }
    # 不应抛
    ok = await persist_signal(fake_db, "btc_eth", signal, notification=alert_client)
    assert ok is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_signal", [None, [], "str", 123])
async def test_persist_signal_nondict_signal_no_raise(
    fake_db: FakeDB, alert_client, bad_signal,
):
    """signal 非 dict（None/list/str/int）时，persist_signal 应不抛异常、
    返回 False，且 notification.send_alert 仍被调用一次（除 schema 校验外
    不走 DB 路径，DB execute 不应被触发）。"""
    # try/except 断言不抛异常，有异常直接 pytest.fail 报红
    try:
        ok = await persist_signal(
            fake_db, "btc_eth", bad_signal, notification=alert_client,
        )
    except Exception as exc:
        pytest.fail(f"非 dict signal 不应抛异常，实际抛出: {exc!r}")

    assert ok is False
    # 告警链路仍可达（except 块走 safe_signal → 仍调用 _try_alert）
    alert_client.send_alert.assert_awaited_once()
    # DB 未被触碰（错误发生在 try 首行 signal.get，早于 db.execute）
    assert fake_db.execute_calls == []


@pytest.mark.asyncio
async def test_persist_signal_dict_with_nondict_symbol_no_raise(
    fake_db: FakeDB, alert_client,
):
    """signal 是 dict 但 symbol 为非字符串（如 int=123）时，
    build_dedup_key 应通过 str() 归一兜底，不抛异常、persist_signal 正常返回 True。

    回归测试：修复前 signal={"symbol":123,...} 走正常 try 路径，signal.get("symbol")
    返回 int → build_dedup_key 内 (symbol or "").strip() 抛 AttributeError → 外层 except
    里 build_dedup_key(safe_signal.get("symbol"), ...) 同样是 int → 二次抛异常逃逸函数，
    违反"绝不 raise"硬不变量。现在 build_dedup_key 内部 str() 兜底后，整条链路都安全。
    """
    signal = {
        "symbol": 123,  # 非 str，是 int
        "direction": "LONG",
        "grade": "A",
        "score": 92,
        "entry_price": 65000.0,
        "trade_success": True,
        "created_at": datetime(2026, 10, 19, 14, 0, 0),
    }

    # 不 raise 是核心断言
    ok = await persist_signal(fake_db, "btc_eth", signal, notification=alert_client)
    # 走正常 try 路径 → 返回 True（写库成功）
    assert ok is True
    # DB execute 被调用一次
    assert len(fake_db.execute_calls) == 1
    # dedup_key 内 symbol 已被 str() 归一 → "123"（int(123).strip() 现在先 str 再 strip）
    _, args = fake_db.execute_calls[0]
    assert args[5] == "123:LONG:2026101914"
    # 告警不应触发（正常落库成功）
    alert_client.send_alert.assert_not_awaited()


# ===================================================================
# 6. cancelled 信号写入并带 skip_reason / reject_reason
# ===================================================================


@pytest.mark.asyncio
async def test_persist_signal_cancelled_with_reject_reason(fake_db: FakeDB):
    """trade_success=False + reject_reason 存在 → status='cancelled'，metadata 含 reject_reason"""
    signal = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "grade": "A",
        "score": 92,
        "entry_price": 65000.0,
        "trade_success": False,
        "reject_reason": "资金拦截：持仓已达上限",
        "created_at": datetime(2026, 10, 19, 14, 0, 0),
        "initial_stop_loss": 64000.0,
    }
    ok = await persist_signal(fake_db, "btc_eth", signal)
    assert ok is True

    _, args = fake_db.execute_calls[0]
    assert args[6] == "cancelled"
    meta = json.loads(args[8])
    assert meta.get("reject_reason") == "资金拦截：持仓已达上限"
    assert meta.get("initial_stop_loss") == 64000.0


@pytest.mark.asyncio
async def test_persist_signal_cancelled_with_skip_reason(fake_db: FakeDB):
    """trade_success=False + skip_reason 存在 → status='cancelled'，metadata 含 skip_reason"""
    signal = {
        "symbol": "ETHUSDT",
        "direction": "SHORT",
        "grade": "C",
        "score": 55,
        "entry_price": 3200.0,
        "trade_success": False,
        "skip_reason": "评分未过阈值",
        "created_at": datetime(2026, 10, 19, 15, 0, 0),
    }
    await persist_signal(fake_db, "btc_eth", signal)
    _, args = fake_db.execute_calls[0]
    assert args[6] == "cancelled"
    meta = json.loads(args[8])
    assert meta.get("skip_reason") == "评分未过阈值"


@pytest.mark.asyncio
async def test_cancelled_metadata_has_both_reasons(fake_db: FakeDB):
    """skip_reason / reject_reason 同时存在 → metadata 两个都保留（策略侧二选一，
    但 persistence 模块不应假设只有一个）"""
    signal = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "grade": "A",
        "score": 92,
        "entry_price": 65000.0,
        "trade_success": False,
        "skip_reason": "频率控制拒绝",
        "reject_reason": "资金拦截",
        "created_at": datetime(2026, 10, 19, 14, 0, 0),
    }
    await persist_signal(fake_db, "btc_eth", signal)
    _, args = fake_db.execute_calls[0]
    meta = json.loads(args[8])
    assert meta.get("skip_reason") == "频率控制拒绝"
    assert meta.get("reject_reason") == "资金拦截"


# ===================================================================
# 7. schema 名安全检查
# ===================================================================


def test_sanitize_schema_rejects_injection():
    """非法 schema 名被拒绝（防 SQL 注入）"""
    # _sanitize_schema 在 persist_signal 内部调用，但 schema 非法会 raise，
    # 被 persist_signal 的 except 块吞掉。这里通过返回 False 验证 schema 非法。
    import asyncio

    class ThrowDB:
        async def execute(self, *args, **kwargs):
            # 不会执行到，因为 schema 校验先炸
            return "OK"

    async def _try():
        return await persist_signal(ThrowDB(), "btc_eth; DROP TABLE users", {})

    result = asyncio.run(_try())
    assert result is False  # 非法 schema → persist_signal 内部吞掉 → 返回 False


# ===================================================================
# 8. 决策时间解析：timestamp 兜底 created_at 缺失场景（fix 验证）
# ===================================================================


class TestResolveDecisionTime:
    def test_timestamp_when_no_created_at(self):
        """信号只有 timestamp 时取 timestamp（修复前恒走 now 兜底）"""
        ts = datetime(2026, 1, 2, 3, 4, 0)
        signal = {"symbol": "BTCUSDT", "timestamp": ts}
        assert _resolve_decision_time(signal) is ts

    def test_created_at_priority_over_timestamp(self):
        """created_at 与 timestamp 同时存在时，created_at 优先"""
        ca = datetime(2026, 1, 1, 10, 0, 0)
        ts = datetime(2026, 1, 2, 3, 4, 0)
        signal = {"created_at": ca, "timestamp": ts}
        assert _resolve_decision_time(signal) is ca

    def test_fallback_to_now_when_both_missing(self):
        """created_at / timestamp 都没有时安全回退到当前北京时间"""
        signal = {"symbol": "BTCUSDT", "direction": "LONG"}
        result = _resolve_decision_time(signal)
        assert isinstance(result, datetime)
        # 允许 ±2 秒内
        diff = abs((result - datetime.now(BEIJING_TZ)).total_seconds())
        assert diff < 2

    def test_non_datetime_values_fallback_safely(self):
        """created_at / timestamp 存在但不是 datetime（如字符串）→ 回退到 now"""
        signal = {"created_at": "2026-01-01 10:00:00", "timestamp": "bad"}
        result = _resolve_decision_time(signal)
        assert isinstance(result, datetime)


@pytest.mark.asyncio
async def test_dedup_key_uses_timestamp_when_no_created_at(fake_db: FakeDB):
    """集成验证：persist_signal 用 timestamp 生成 dedup_key 的小时粒度

    回归测试——修复前 dedup_key 的时间部分恒为 now(BEIJING_TZ)。
    """
    ts = datetime(2026, 1, 2, 3, 4, 0)  # 固定历史时间
    signal = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "grade": "A",
        "score": 92,
        "entry_price": 65000.0,
        "trade_success": True,
        "timestamp": ts,  # 只有 timestamp，无 created_at
    }
    await persist_signal(fake_db, "btc_eth", signal)
    _, args = fake_db.execute_calls[0]
    assert args[5] == "BTCUSDT:LONG:2026010203"  # 小时部分来自 timestamp


# ===================================================================
# 9. metadata 值：嵌套 dict/list 含 datetime + Decimal 序列化
# ===================================================================


@pytest.mark.asyncio
async def test_json_default_nested_and_decimal(fake_db: FakeDB):
    """signal 含嵌套 dict（内嵌 datetime）、list（含 datetime）、Decimal →
    persist_signal 写库后 metadata JSON 中 datetime 转 isoformat、Decimal 转字符串"""
    inner_dt = datetime(2026, 5, 1, 8, 30, 0)
    outer_dt = datetime(2026, 5, 2, 10, 0, 0)
    signal = {
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "grade": "A",
        "score": 92,
        "entry_price": 65000.0,  # 主列，不进 metadata
        "trade_success": True,
        "created_at": datetime(2026, 10, 19, 14, 0, 0),
        # 嵌套 dict，里面还有 datetime
        "extra": {"inner_time": inner_dt, "tag": "risky"},
        # 含 datetime 的 list
        "review_times": [inner_dt, outer_dt],
        # Decimal 作为独立字段（非主列键，应进 metadata）
        "fee_rate": Decimal("0.001"),
        "risk_limit": Decimal("12.50"),
    }
    await persist_signal(fake_db, "btc_eth", signal)
    _, args = fake_db.execute_calls[0]
    meta_str = args[8]
    meta = json.loads(meta_str)

    # 嵌套 dict 内 datetime 已转 isoformat
    assert meta["extra"]["inner_time"] == inner_dt.isoformat()
    assert meta["extra"]["tag"] == "risky"
    # list 内 datetime 已转 isoformat
    assert meta["review_times"][0] == inner_dt.isoformat()
    assert meta["review_times"][1] == outer_dt.isoformat()
    # Decimal → json.dumps default 走 str() → 字符串
    assert meta["fee_rate"] == "0.001"
    assert meta["risk_limit"] == "12.50"
    # 主列不应进 metadata
    assert "entry_price" not in meta
    assert "symbol" not in meta


# ===================================================================
# 10. _json_default 异常兜底分支
# ===================================================================


def test_json_default_exception_branch():
    """_json_default 对 __str__ 抛异常的对象，返回 '<unserializable>' 兜底"""

    class _StrRaiser:
        def __str__(self):
            raise RuntimeError("I cannot be stringified")

    result = _json_default(_StrRaiser())
    assert result == "<unserializable>"


def test_json_default_datetime_branch():
    """_json_default 直接接收 datetime 对象 → 走 datetime 分支返回 isoformat"""
    dt = datetime(2026, 1, 2, 3, 4, 5)
    assert _json_default(dt) == dt.isoformat()


# ===================================================================
# 11. schema 名安全检查：非 str 输入分支
# ===================================================================


@pytest.mark.parametrize("bad_value", [None, 123, 3.14])
def test_sanitize_schema_rejects_non_str(bad_value):
    """_sanitize_schema 对非 str 输入直接抛 ValueError"""
    with pytest.raises(ValueError):
        _sanitize_schema(bad_value)


# ===================================================================
# 12. build_dedup_key 小时边界：13:59:59 与 14:00:00 生成不同 key
# ===================================================================


def test_dedup_key_hour_boundary():
    """同分钟同方向，仅跨小时 → dedup_key 小时部分不同"""
    k1 = build_dedup_key(
        "BTCUSDT", "LONG", datetime(2026, 1, 2, 13, 59, 59),
    )
    k2 = build_dedup_key(
        "BTCUSDT", "LONG", datetime(2026, 1, 2, 14, 0, 0),
    )
    assert k1 == "BTCUSDT:LONG:2026010213"
    assert k2 == "BTCUSDT:LONG:2026010214"
    assert k1 != k2
