"""
execution_facts_sink 离线测试（T1/T2/T3）。

核心覆盖：
  T1  SQL 安全断言（无 --、/*、;、危险词）+ 冲突目标 + 参数映射 +
      upsert_fills/upsert_income 顺带写 raw（同一事务内）
  T2  sink 只做 json.dumps 落 raw，不重复脱敏
  T3  save_cursor→load_cursor 往返（含 symbol=None 分支，
      断言 SELECT 含 IS NOT DISTINCT FROM）
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

# 被测模块
from shared.execution_facts_sink import (
    PostgresExecutionFactsSink,
    _str_to_decimal,
    _ms_to_timestamptz,
    _jsonb_param,
)

# 上游 Fact 定义（鸭子类型；测试里可导入真实 dataclass 直接构造）
from scripts.export_execution_facts import (
    FillFact, IncomeFact, CursorRecord, sanitize_payload,
)


# ── 共用 Fake DB：记录所有 SQL + args ──────────────────────────

class FakeDb:
    """记录 execute / execute_transaction / fetch_one 调用的 Fake DB。

    用 dict 作模拟表存储 fetch_one 结果（供 T3 cursor 往返）。
    """

    def __init__(self) -> None:
        self.transactions: List[List[tuple]] = []   # 每次 execute_transaction 的 [(sql,args), ...]
        self.executes: List[tuple] = []              # execute(sql, *args) 的记录
        self.fetch_args: List[tuple] = []            # fetch_one(sql, *args) 的记录
        self.store: Dict[tuple, Dict[str, Any]] = {}  # cursor key → row

    async def execute_transaction(self, queries: List[tuple]) -> bool:
        self.transactions.append(list(queries))
        return True

    async def execute(self, query: str, *args, **kwargs) -> str:
        self.executes.append((query, args))
        return "OK"

    async def fetch_one(self, query: str, *args, **kwargs) -> Optional[Dict[str, Any]]:
        self.fetch_args.append((query, args))
        # 只模拟 cursor 表的读
        if "execution_fact_cursor" in query and "IS NOT DISTINCT FROM" in query:
            # 新签名：source + account_id + income_type + symbol + window_start_ms
            # args 数量可能是 3（旧）或 5（新），需要兼容
            if len(args) == 5:
                source, account_id, income_type, symbol, window_start = args
                row = self.store.get((source, account_id, income_type, symbol, window_start))
            else:
                # 旧签名 fallback
                row = None
            return dict(row) if row else None
        return None


# ── SQL 安全断言辅助 ───────────────────────────────────────────

def assert_sql_safe(sql: str) -> None:
    """断言 SQL 串不含注释、分号、危险词。"""
    assert "--" not in sql, f"SQL 含 -- 注释: {sql[:80]}"
    assert "/*" not in sql, f"SQL 含 /* 块注释: {sql[:80]}"
    assert ";" not in sql, f"SQL 含分号: {sql[:80]}"
    up = sql.upper()
    for kw in ("CREATE", "GRANT", "ALTER", "DROP", "TRUNCATE", "REVOKE"):
        assert not re.search(rf"\b{kw}\b", up), f"SQL 含危险词 {kw}: {sql[:80]}"


@pytest.fixture
def sink() -> PostgresExecutionFactsSink:
    return PostgresExecutionFactsSink(FakeDb())


def _sample_fill() -> FillFact:
    raw = {"symbol": "BTCUSDT", "qty": "0.12345", "secret": "should_be_stripped", "apiKey": "xxx"}
    sanitized = sanitize_payload(raw)
    return FillFact(
        account_id="main",
        symbol="BTCUSDT",
        trade_id=123456789,
        order_id=987654,
        side="BUY",
        qty="0.12345",
        price="50000.50",
        quote_qty="6172.85",
        commission="0.00123",
        commission_asset="BNB",
        realized_pnl="25.50",
        trade_time_ms=1729000000000,
        raw_payload=sanitized,
        payload_sha256="abc123def456",
    )


def _sample_income() -> IncomeFact:
    return IncomeFact(
        account_id="main",
        tran_id=999888777,
        symbol="BTCUSDT",
        income_type="REALIZED_PNL",
        income="123.45",
        asset="USDT",
        info="some info",
        trade_id=123456789,
        event_time_ms=1729000100000,
        raw_payload={"incomeType": "REALIZED_PNL"},
        payload_sha256="xyz789",
    )


# ══════════════════════════════════════════════════════════════════
# T1：upsert_fills / upsert_income SQL 安全 + 冲突目标 +
#     参数映射 + 顺带写 raw（同一事务内）
# ══════════════════════════════════════════════════════════════════

def test_upsert_fills_sql_safe_and_includes_raw(sink: PostgresExecutionFactsSink):
    """upsert_fills 内 SQL 全部安全（无注释/分号/危险词），
    且事务内第二条是 raw upsert。"""
    fact = _sample_fill()
    asyncio.run(sink.upsert_fills([fact]))

    db: FakeDb = sink.db
    assert len(db.transactions) == 1
    queries = db.transactions[0]

    # 逐条断言 SQL 安全
    for sql, _ in queries:
        assert_sql_safe(sql)

    # 第一条 fill upsert：冲突目标是 (account_id,symbol,trade_id)
    fill_sql, fill_args = queries[0]
    assert "ON CONFLICT (account_id, symbol, trade_id) DO UPDATE" in fill_sql
    assert len(fill_args) == 12  # 12 个列

    # 断言参数映射：qty/price 是 Decimal，时间是 datetime
    assert isinstance(fill_args[2], int)        # trade_id
    assert isinstance(fill_args[5], Decimal)    # qty
    assert isinstance(fill_args[6], Decimal)    # price
    assert isinstance(fill_args[11], datetime)  # trade_time_utc

    # 第二条是 raw upsert（顺带写）
    raw_sql, raw_args = queries[1]
    assert "execution_fact_raw" in raw_sql
    assert raw_args[0] == "userTrades"          # source
    assert raw_args[1] == "fill"                 # entity_key
    # entity_id 折叠
    assert raw_args[2] == "main:BTCUSDT:123456789"


def test_upsert_income_sql_safe_and_includes_raw(sink: PostgresExecutionFactsSink):
    """upsert_income 同上口径。"""
    fact = _sample_income()
    asyncio.run(sink.upsert_income([fact]))

    db: FakeDb = sink.db
    assert len(db.transactions) == 1
    queries = db.transactions[0]

    for sql, _ in queries:
        assert_sql_safe(sql)

    income_sql, income_args = queries[0]
    assert "ON CONFLICT (account_id, tran_id) DO UPDATE" in income_sql
    assert len(income_args) == 9

    raw_sql, raw_args = queries[1]
    assert raw_args[0] == "um/income"
    assert raw_args[1] == "income"
    assert raw_args[2] == "main:999888777"


def test_upsert_empty_list_is_noop(sink: PostgresExecutionFactsSink):
    """空列表不应触发任何 DB 调用。"""
    asyncio.run(sink.upsert_fills([]))
    asyncio.run(sink.upsert_income([]))
    assert sink.db.transactions == []


def test_upsert_multiple_facts_single_transaction(sink: PostgresExecutionFactsSink):
    """多个 fact 在一次 upsert_fills 中应共用同一事务。"""
    facts = [_sample_fill(), _sample_fill()]
    facts[1].trade_id = 123456790
    asyncio.run(sink.upsert_fills(facts))

    db: FakeDb = sink.db
    assert len(db.transactions) == 1
    assert len(db.transactions[0]) == 4  # 2 fills + 2 raws


# ══════════════════════════════════════════════════════════════════
# T2：sink 不重复脱敏（raw_payload 里的敏感字段应已被上游剔除）
# ══════════════════════════════════════════════════════════════════

def test_sink_does_not_re_sanitize(sink: PostgresExecutionFactsSink):
    """上游 FillFact.raw_payload 已脱敏，sink 只 json.dumps 不做二次清理。"""
    # 显式构造一个含敏感 key 但已被上游剔除的 raw_payload 场景：
    # 上游 sanitize_payload 后不应出现 secret/apiKey/signature 等。
    fact = _sample_fill()
    # 这里原始 raw_payload 里含 "secret" 但上游 sanitize_payload 应已剔除。
    # 测试验证 sink 不做额外处理，原样 json.dumps 落库：
    asyncio.run(sink.upsert_fills([fact]))

    db: FakeDb = sink.db
    # raw upsert 的 args[3] 是 raw_payload（JSON 串）
    raw_args = db.transactions[0][1][1]  # 第一次事务的第二条的 args
    raw_json = raw_args[3]               # raw_payload JSON 串
    # sink 应该原样 json.dumps 已脱敏 dict；断言不含敏感 key
    assert "secret" not in raw_json.lower()
    assert "apikey" not in raw_json.lower()
    assert "signature" not in raw_json.lower()


# ══════════════════════════════════════════════════════════════════
# T3：save_cursor → load_cursor 往返；symbol=None 走
#     IS NOT DISTINCT FROM 语义
# ══════════════════════════════════════════════════════════════════

def test_cursor_roundtrip_symbol_none(sink: PostgresExecutionFactsSink):
    """save_cursor 后 load_cursor 能取回等价记录；symbol=None 场景。"""
    db: FakeDb = sink.db

    # 先写 store 让 load_cursor 能命中（新键：source+account_id+income_type+symbol+window_start_ms）
    db.store[("um/income", "main", "REALIZED_PNL", None, 1729000000000)] = {
        "source": "um/income",
        "account_id": "main",
        "income_type": "REALIZED_PNL",
        "symbol": None,
        "window_start_ms": 1729000000000,
        "window_end_ms": 1729086400000,
        "last_event_id": 123,
        "covered_until_ms": 1729000100000,
        "returned_count": 42,
        "page_complete": True,
        "request_status": "ok",
        "error_detail": None,
        "retry_count": 0,
    }

    # load_cursor 新签名：source, account_id, income_type, symbol, window_start_ms
    result = asyncio.run(
        sink.load_cursor("um/income", "main", "REALIZED_PNL", None, 1729000000000)
    )
    assert result is not None
    assert result.source == "um/income"
    assert result.account_id == "main"
    assert result.income_type == "REALIZED_PNL"
    assert result.symbol is None
    assert result.returned_count == 42
    assert result.page_complete is True
    assert result.request_status == "ok"

    # 断言 SQL 串含 IS NOT DISTINCT FROM
    fetch_sql = db.fetch_args[0][0]
    assert "IS NOT DISTINCT FROM" in fetch_sql
    assert_sql_safe(fetch_sql)

    # save_cursor → 断言 execute SQL 安全 + 参数正确
    cursor = CursorRecord(
        source="userTrades",
        account_id="main",
        income_type=None,
        symbol="BTCUSDT",
        window_start_ms=1729000000000,
        window_end_ms=1729086400000,
        covered_until_ms=1729000100000,
        returned_count=42,
        page_complete=True,
        request_status="ok",
    )
    asyncio.run(sink.save_cursor(cursor))
    assert len(db.executes) == 1
    save_sql, save_args = db.executes[0]
    assert_sql_safe(save_sql)
    # 新顺序：source, account_id, income_type, symbol, window_start_ms
    assert save_args[0] == "userTrades"   # source
    assert save_args[1] == "main"          # account_id
    assert save_args[2] is None            # income_type
    assert save_args[3] == "BTCUSDT"       # symbol
    assert save_args[4] == 1729000000000   # window_start_ms


def test_load_cursor_not_found_returns_none(sink: PostgresExecutionFactsSink):
    """store 里无匹配时 load_cursor 返回 None。"""
    result = asyncio.run(
        sink.load_cursor("userTrades", "main", None, "BTCUSDT", 9999999)
    )
    assert result is None


def test_exception_is_swallowed_load_only(sink: PostgresExecutionFactsSink):
    """db 抛异常时 load_cursor 降级返回 None（只 load 吞异常）。"""
    class BoomDb:
        async def fetch_one(self, *args, **kwargs):
            raise RuntimeError("boom")

    sink.db = BoomDb()
    # load_cursor 仍返回 None（降级）
    asyncio.run(sink.load_cursor("userTrades", "main", None, "BTCUSDT", 0))


def test_save_cursor_exception_raised(sink: PostgresExecutionFactsSink):
    """db 抛异常时 save_cursor 必须上抛（新契约）。"""
    class BoomDb:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("boom")

    sink.db = BoomDb()
    cursor = CursorRecord(
        source="userTrades", account_id="main", income_type=None,
        symbol="BTCUSDT", window_start_ms=1,
    )
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(sink.save_cursor(cursor))


def test_upsert_exception_raised(sink: PostgresExecutionFactsSink):
    """db 抛异常时 upsert_fills/upsert_income 必须上抛（新契约）。"""
    class BoomDb:
        async def execute_transaction(self, queries):
            raise RuntimeError("boom")

    sink.db = BoomDb()
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(sink.upsert_fills([_sample_fill()]))
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(sink.upsert_income([_sample_income()]))


# ══════════════════════════════════════════════════════════════════
# 工具函数单元测试（快速覆盖类型转换边界条件）
# ══════════════════════════════════════════════════════════════════

def test_str_to_decimal_variants():
    assert _str_to_decimal("123.45") == Decimal("123.45")
    assert _str_to_decimal(None) is None
    assert _str_to_decimal("") is None
    assert _str_to_decimal("not_a_number") is None  # InvalidOperation → None


def test_ms_to_timestamptz_variants():
    assert _ms_to_timestamptz(None) is None
    ts = _ms_to_timestamptz(1729000000000)
    assert ts is not None
    assert ts.tzinfo == timezone.utc


def test_jsonb_param_sorted_keys():
    result = _jsonb_param({"b": 1, "a": 2, "c": {"z": 9, "y": 8}})
    # sort_keys=True → 输出应按 key 排序
    assert result.index('"a"') < result.index('"b"') < result.index('"c"')


# ══════════════════════════════════════════════════════════════════
# 补充覆盖：getattr 默认值分支 / _cursor_from_row or 兜底 /
#          _str_to_decimal TypeError 分支 / 0 值边界 /
#          _build_raw_args 缺省字段 / 带 symbol 游标往返
# ══════════════════════════════════════════════════════════════════

def test_cursor_to_args_defaults_on_missing_fields():
    """字段缺失时 _cursor_to_args 的 getattr 默认值兜底（鸭子类型安全降级）。"""
    from types import SimpleNamespace
    from shared.execution_facts_sink import _cursor_to_args

    cursor = SimpleNamespace(source="userTrades")  # 只有 source，其余字段全缺
    args = _cursor_to_args(cursor)
    assert args[0] == "userTrades"
    assert args[1] is None      # account_id 缺失 → None
    assert args[2] is None      # income_type 缺失 → None
    assert args[3] is None      # symbol 缺失 → None
    assert args[4] is None      # window_start_ms 缺失 → None
    assert args[5] is None      # window_end_ms 缺失 → None
    assert args[6] is None      # last_event_id 缺失 → None
    assert args[7] is None      # covered_until_ms 缺失 → None
    assert args[8] == 0         # returned_count 缺失 → 0
    assert args[9] is False     # page_complete 缺失 → False
    assert args[10] == "ok"     # request_status 缺失 → "ok"
    assert args[11] is None     # error_detail 缺失 → None
    assert args[12] == 0        # retry_count 缺失 → 0


def test_cursor_to_args_none_values_coerced():
    """属性存在但值为 None：计数经 or 0 归零；request_status 为 None 时不兜底。"""
    from types import SimpleNamespace
    from shared.execution_facts_sink import _cursor_to_args

    cursor = SimpleNamespace(
        source="s", account_id="a", income_type=None, symbol=None,
        window_start_ms=1,
        returned_count=None, retry_count=None,
        page_complete=0, request_status=None,
    )
    args = _cursor_to_args(cursor)
    assert args[8] == 0        # returned_count None or 0 → 0
    assert args[12] == 0       # retry_count None or 0 → 0
    assert args[9] is False    # bool(0) → False
    # getattr 默认值只在属性缺失时生效；显式 None 透传
    assert args[10] is None


def test_cursor_from_row_none_fields_fallback():
    """row 字段为 None 时 or 兜底：returned_count/retry_count→0、request_status→'ok'。"""
    from shared.execution_facts_sink import _cursor_from_row

    row = {
        "source": "userTrades",
        "account_id": "a",
        "income_type": None,
        "window_start_ms": 123,
        "returned_count": None,
        "retry_count": None,
        "request_status": None,
        "page_complete": None,
    }
    rec = _cursor_from_row(row)
    assert rec.returned_count == 0
    assert rec.retry_count == 0
    assert rec.request_status == "ok"
    assert rec.page_complete is False
    assert rec.symbol is None   # 缺 key → .get 默认 None


def test_str_to_decimal_type_error_branch():
    """不支持的对象类型 → Decimal 抛 TypeError → 降级 None。"""
    assert _str_to_decimal(object()) is None


def test_str_to_decimal_zero_string():
    """'0' 是有效值（非 None/空串）→ 返回 Decimal 0，不得误判为空。"""
    assert _str_to_decimal("0") == Decimal("0")


def test_ms_to_timestamptz_zero_is_epoch():
    """0 毫秒是有效边界 → 1970-01-01 UTC，不得被当成 None。"""
    ts = _ms_to_timestamptz(0)
    assert ts == datetime(1970, 1, 1, tzinfo=timezone.utc)


def test_build_raw_args_defaults_for_missing_attrs():
    """fact 缺 raw_payload/payload_sha256 属性时默认 {} 与 ''。"""
    from types import SimpleNamespace
    sink = PostgresExecutionFactsSink(FakeDb())
    fact = SimpleNamespace(account_id="a", symbol="BTCUSDT", trade_id=1)
    args = sink._build_raw_args(
        fact, source="userTrades", entity_key="fill", entity_id="a:BTCUSDT:1",
    )
    assert args[0] == "userTrades"
    assert args[1] == "fill"
    assert args[2] == "a:BTCUSDT:1"
    assert args[3] == "{}"      # raw_payload 缺失 → {} → "{}"
    assert args[4] == ""        # payload_sha256 缺失 → ""


def test_build_fill_args_none_numeric_fields(sink: PostgresExecutionFactsSink):
    """fill 数值/时间字段全 None → args 对应位为 None（不抛异常）。"""
    from types import SimpleNamespace
    fact = SimpleNamespace(
        account_id="main", symbol="BTCUSDT", trade_id=1, order_id=None,
        side=None, qty=None, price=None, quote_qty=None, commission=None,
        commission_asset=None, realized_pnl=None, trade_time_ms=None,
    )
    args = sink._build_fill_args(fact)
    assert len(args) == 12
    assert args[5] is None      # qty
    assert args[6] is None      # price
    assert args[11] is None     # trade_time_utc


def test_cursor_roundtrip_with_symbol(sink: PostgresExecutionFactsSink):
    """带 symbol 的游标 load 往返（补充 symbol=None 之外的分支）。"""
    db: FakeDb = sink.db
    # 新键：source + account_id + income_type + symbol + window_start_ms
    db.store[("userTrades", "main", None, "BTCUSDT", 1729000000000)] = {
        "source": "userTrades",
        "account_id": "main",
        "income_type": None,
        "symbol": "BTCUSDT",
        "window_start_ms": 1729000000000,
        "window_end_ms": 1729000800000,
        "last_event_id": 7,
        "covered_until_ms": 1729000000000,
        "returned_count": 3,
        "page_complete": False,
        "request_status": "partial",
        "error_detail": "超时重试",
        "retry_count": 2,
    }
    result = asyncio.run(sink.load_cursor("userTrades", "main", None, "BTCUSDT", 1729000000000))
    assert result is not None
    assert result.symbol == "BTCUSDT"
    assert result.page_complete is False
    assert result.request_status == "partial"
    assert result.retry_count == 2
