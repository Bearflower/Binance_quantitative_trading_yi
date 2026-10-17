"""
D1 只读采集（execution_facts）核心逻辑的**纯离线**测试。

不联网、不连生产库、不调用真实交易所接口——所有 API 都通过 FakeClient 注入，
所有落库都通过 FakeSink 注入。覆盖脚本的：
  - 分页完整性（1501 = 1000 + 501 两页）
  - 去重（重复页同 trade_id 只计一次）
  - 边界同毫秒多笔不丢（键是整数 id，不靠时间）
  - 断点恢复（通过 FakeSink.load_cursor 注入游标续跑）
  - 不同币相同 trade_id 不冲突（复合键含 symbol）
  - 空页 / 请求失败（空页 page_complete=TRUE；失败记录 request_status + 不丢已取数据）
  - 脱敏（apiKey / signature / secretKey 被剔除；非敏感字段保留）
  - max_pages 守卫（超过强制终止，cursor 落 error_detail="翻页超过 max_pages 守卫"）
  - income 空窗口（调用方完全没有收入数据时 page_complete=TRUE 不崩溃）
"""
from __future__ import annotations

import hashlib
import pytest

from scripts.export_execution_facts import (
    ExecutionFactExporter,
    FillFact,
    IncomeFact,
    CursorRecord,
    sanitize_payload,
    compute_sha256,
    is_sensitive_key,
    _make_cursor,
)

pytestmark = pytest.mark.asyncio

# ────────────────────────────────────────────────────────────────────
# 测试数据生成
# ────────────────────────────────────────────────────────────────────

_SYMBOL = "BTCUSDT"
_WINDOW_START = 1700000000000
_WINDOW_END = 1700000300000
_ACCOUNT = "test-account"


def _fake_fill_row(trade_id: int, time_ms: int, symbol: str = _SYMBOL, **overrides):
    """构造一条 userTrades 响应字典（币安**真实字段名** buyer/maker，不是 isBuyer/isMaker）。"""
    row = {
        "id": trade_id,
        "orderId": 10000 + trade_id,
        "symbol": symbol,
        "side": "BUY" if trade_id % 2 == 0 else "SELL",
        "qty": "0.1",
        "price": "50000.0",
        "quoteQty": "5000.0",
        "commission": "0.5",
        "commissionAsset": "USDT",
        "realizedPnl": "1.23",
        "time": time_ms,
        "buyer": True,
        "maker": False,
    }
    row.update(overrides)
    return row


def _fake_income_row(tran_id: int, time_ms: int, **overrides):
    """构造一条 um/income 响应字典。"""
    row = {
        "tranId": tran_id,
        "symbol": _SYMBOL,
        "incomeType": "REALIZED_PNL",
        "income": "10.00",
        "asset": "USDT",
        "info": "",
        "tradeId": 20000 + tran_id,
        "time": time_ms,
    }
    row.update(overrides)
    return row


# ────────────────────────────────────────────────────────────────────
# Fake API 客户端（实现 get_user_trades + get_income_history）
# ────────────────────────────────────────────────────────────────────

class _FakeClient:
    """按请求参数返回预设数据的假客户端。

    用 records_by_symbol 存 userTrades（按 time 升序），
    用 income_records 存 um/income。支持 start_time/end_time/limit 切片。
    同时提供一个 hook：可配置哪次请求抛异常（断点恢复测试用）。
    """

    def __init__(
        self,
        records_by_symbol: dict[str, list[dict]],
        income_records: list[dict] | None = None,
        *,
        throw_on_call: int | None = None,   # 第几次调用抛异常（含两个 endpoint 的总次数）
        throw_exception: Exception | None = None,
    ):
        self._records_by_symbol = records_by_symbol
        self._income_records = income_records or []
        self._call_count = 0
        self._throw_on_call = throw_on_call
        self._throw_exc = throw_exception or RuntimeError("模拟网络超时")

    async def get_user_trades(
        self, symbol: str, *, start_time=None, end_time=None, limit=None,
        from_id=None,
    ):
        self._call_count += 1
        if self._throw_on_call is not None and self._call_count >= self._throw_on_call:
            raise self._throw_exc
        rows = self._records_by_symbol.get(symbol, [])
        return self._filter_and_limit(rows, start_time, end_time, limit,
                                      time_key="time", from_id=from_id)

    async def get_income_history(
        self, *, start_time=None, end_time=None, limit=None, income_type=None,
    ):
        self._call_count += 1
        if self._throw_on_call is not None and self._call_count >= self._throw_on_call:
            raise self._throw_exc
        rows = self._income_records
        if income_type:
            rows = [r for r in rows if r.get("incomeType") == income_type]
        return self._filter_and_limit(rows, start_time, end_time, limit,
                                      time_key="time")

    @staticmethod
    def _filter_and_limit(rows, start_time, end_time, limit, *, time_key: str,
                          from_id: int | None = None):
        """按时间窗或 from_id 切片（互斥，对齐 PM UM userTrades 契约）。

        - from_id 与 start/end_time 同现 → ValueError（真实接口互斥约束）
        - 仅 from_id → id > from_id（ID 续传路径，不按时间过滤）
        - 仅时间 → start <= time < end（时间定位路径）
        """
        if from_id is not None and (start_time is not None or end_time is not None):
            raise ValueError(
                "from_id 与 start_time/end_time 互斥，不能同时传入"
            )
        if from_id is not None:
            out = [r for r in rows
                   if r.get("id") is not None and r["id"] > from_id]
        else:
            out = []
            for r in rows:
                t = r.get(time_key)
                if start_time is not None and t is not None and t < start_time:
                    continue
                if end_time is not None and t is not None and t >= end_time:
                    continue
                out.append(r)
        if limit is not None:
            out = out[:limit]
        return out


# ────────────────────────────────────────────────────────────────────
# Fake 数据下沉（把调用参数存起来，测试断言用）
# ────────────────────────────────────────────────────────────────────

class _FakeSink:
    """把 upsert_fills / upsert_income / save_cursor 收到的参数都存起来。
    load_cursor 返回构造时提供的对象（断点恢复测试用）。
    """

    def __init__(self, initial_cursors: dict | None = None):
        self.fills_upserted: list = []
        self.income_upserted: list = []
        self.cursors_saved: list = []
        self._initial_cursors = initial_cursors or {}

    async def upsert_fills(self, fills):
        self.fills_upserted.extend(fills)

    async def upsert_income(self, income):
        self.income_upserted.extend(income)

    async def save_cursor(self, cursor: CursorRecord):
        self.cursors_saved.append(cursor)

    async def load_cursor(self, source, account_id, income_type, symbol, window_start_ms):
        key = (source, account_id, income_type, symbol, window_start_ms)
        return self._initial_cursors.get(key)


# ────────────────────────────────────────────────────────────────────
# Fixture
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def symbol_bundle_fill_two_pages():
    """1501 条：第 1 页满 1000（id 1-1000），第 2 页 501（id 1001-1501）。
    时间按毫秒递增：每页 1000 条时间相同窗口，能触发满页继续翻。
    """
    rows = []
    for i in range(1, 1502):
        rows.append(_fake_fill_row(i, _WINDOW_START + i))  # 每条时间 +1ms，保证递增
    return {_SYMBOL: rows}


# ────────────────────────────────────────────────────────────────────
# T1: 分页完整性 —— 1501 条全部取得，无遗漏
# ────────────────────────────────────────────────────────────────────

async def test_t1_1501_records_all_collected(symbol_bundle_fill_two_pages):
    """1501 条 = 两页翻完后 trade_id 1..1501 全覆盖、无遗漏。"""
    client = _FakeClient(symbol_bundle_fill_two_pages)
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000,
    )
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END + 10000)

    ids = sorted(f.trade_id for f in result.records)
    assert len(ids) == 1501
    assert ids[0] == 1
    assert ids[-1] == 1501
    assert ids == list(range(1, 1502))        # 完全连续无缺口
    assert result.page_complete is True
    assert result.errors == []


# ────────────────────────────────────────────────────────────────────
# T2: 重复页不重复计入（Set 去重）
# ────────────────────────────────────────────────────────────────────

async def test_t2_overlap_page_dedup(symbol_bundle_fill_two_pages):
    """让第二页故意带回第一页末尾的重叠 id（990..1000），去重后总数 = 1490。"""
    rows = symbol_bundle_fill_two_pages[_SYMBOL]
    # 构造一个专门的 bundle：第一页 id 1-1000，第二页 id 990-1490（重叠 990-1000 共 11 条）
    overlapping = rows[:1000] + rows[989:1490]
    # 按时间升序排，保证 FakeClient._filter_and_limit 切片后先出 1-1000，再出 990-1490
    overlapping.sort(key=lambda r: r["time"])
    bundle = {_SYMBOL: overlapping}

    client = _FakeClient(bundle)
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000,
    )
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END + 20000)

    seen_ids = {f.trade_id for f in result.records}
    # 去重后：第一页 1-1000 共 1000 条；第二页 990-1490 独有部分为 1001-1490 共 490 条
    # 合计 1000 + 490 = 1490
    assert len(seen_ids) == 1490
    # 确认 990 只出现一次（去重键是整数 trade_id，重复被 Set 吃掉）
    assert sum(1 for f in result.records if f.trade_id == 990) == 1
    assert result.page_complete is True

    # 进一步直接断言：所有 trade_id 都唯一
    assert len(result.records) == len(seen_ids)


# ────────────────────────────────────────────────────────────────────
# T3: 边界同毫秒事件不丢（多笔 time 相同）
# ────────────────────────────────────────────────────────────────────

async def test_t3_same_millisecond_events_preserved():
    """5 笔成交 time 完全相同（同一毫秒）但 id 不同，全部保留。
    证明幂等键是整数 id，不依赖时间。
    """
    fixed_time = 1700000123456
    rows = [
        _fake_fill_row(trade_id=i, time_ms=fixed_time)
        for i in (10001, 10002, 10003, 10004, 10005)
    ]
    # 再加一些其他时间的成交，凑到超过 page_limit（让 FakeClient 翻页）
    extra = [_fake_fill_row(trade_id=i, time_ms=fixed_time + i) for i in range(100, 1100)]
    bundle = {_SYMBOL: rows + extra}

    client = _FakeClient(bundle)
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=500,  # 小 limit 强制翻页
    )
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END + 60000)

    same_ms = [f for f in result.records if f.trade_time_ms == fixed_time]
    assert len(same_ms) == 5
    assert {f.trade_id for f in same_ms} == {10001, 10002, 10003, 10004, 10005}


# ────────────────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────────────────
# T4: 断点恢复 —— 通过 FakeSink.load_cursor 注入真实断点游标续跑
# ────────────────────────────────────────────────────────────────────

async def _t4_setup_and_broken_run(bundle):
    """T4 setup：一次性完整采集 + 中途抛错采集。返回 full_ids, broken_result, broken_sink。"""
    full_client = _FakeClient(bundle)
    full_exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=full_client, page_limit=1000,
    )
    full_result = await full_exporter.export_fills(
        _SYMBOL, _WINDOW_START, _WINDOW_END + 10000,
    )
    full_ids = {f.trade_id for f in full_result.records}

    broken_sink = _FakeSink()
    broken_client = _FakeClient(bundle, throw_on_call=2)
    broken_exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=broken_client, page_limit=1000,
        sink=broken_sink,
    )
    broken_result = await broken_exporter.export_fills(
        _SYMBOL, _WINDOW_START, _WINDOW_END + 10000,
    )
    return full_ids, broken_result, broken_sink


async def test_t4_resume_via_fake_sink_load_cursor(symbol_bundle_fill_two_pages):
    """第 2 次请求抛异常。中途中断后通过 FakeSink.load_cursor 续跑，结果集覆盖完整。"""
    full_ids, broken_result, broken_sink = await _t4_setup_and_broken_run(
        symbol_bundle_fill_two_pages,
    )

    # 中途抛错：已取前 1000 条完整，游标 request_status=error
    assert len(broken_result.records) == 1000
    assert broken_result.errors != []
    assert broken_result.cursor.request_status == "error"
    assert broken_result.cursor.covered_until_ms is not None
    assert broken_result.cursor.covered_until_ms > _WINDOW_START
    assert len(broken_sink.cursors_saved) >= 1

    # 续跑：把 broken 游标注入新 sink，新 exporter 通过 sink.load_cursor 取回
    resume_sink = _FakeSink(initial_cursors={
        ("userTrades", _SYMBOL, _WINDOW_START): broken_result.cursor,
    })
    resume_client = _FakeClient(symbol_bundle_fill_two_pages)
    resume_exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=resume_client, page_limit=1000,
        sink=resume_sink,
    )
    resume_result = await resume_exporter.export_fills(
        _SYMBOL, _WINDOW_START, _WINDOW_END + 10000,
    )
    resume_ids = {f.trade_id for f in resume_result.records}

    # resume 至少拿到 id>1000；broken + resume 的并集覆盖 full_ids
    assert resume_result.page_complete is True
    assert resume_ids & set(range(1001, 1502)) == set(range(1001, 1502))
    broken_ids = {f.trade_id for f in broken_result.records}
    assert broken_ids == set(range(1, 1001))
    assert broken_ids | resume_ids == full_ids
    assert resume_sink.cursors_saved, "续跑应至少落一次完整游标"


# ────────────────────────────────────────────────────────────────────
# T5: 不同币相同 trade_id 不冲突（复合键含 symbol）
# ────────────────────────────────────────────────────────────────────

async def test_t5_same_trade_id_different_symbols_both_kept():
    """BTCUSDT 与 ETHUSDT 同时存在 id=100 的成交，断言两个都被保留（复合键）。"""
    shared_time = _WINDOW_START + 5000
    bundle = {
        "BTCUSDT": [_fake_fill_row(100, shared_time, "BTCUSDT")],
        "ETHUSDT": [_fake_fill_row(100, shared_time, "ETHUSDT")],
    }
    client = _FakeClient(bundle)
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000,
    )

    btc = await exporter.export_fills("BTCUSDT", _WINDOW_START, _WINDOW_END)
    eth = await exporter.export_fills("ETHUSDT", _WINDOW_START, _WINDOW_END)

    btc_ids = {(f.symbol, f.trade_id) for f in btc.records}
    eth_ids = {(f.symbol, f.trade_id) for f in eth.records}
    assert btc_ids == {("BTCUSDT", 100)}
    assert eth_ids == {("ETHUSDT", 100)}


# ────────────────────────────────────────────────────────────────────
# T6a: 零成交 / 空页不报错且 page_complete=TRUE
# ────────────────────────────────────────────────────────────────────

async def test_t6a_empty_returns_page_complete_true_no_errors():
    bundle = {_SYMBOL: []}       # 空
    client = _FakeClient(bundle)
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000,
    )
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    assert result.records == []
    assert result.errors == []
    assert result.page_complete is True
    assert result.cursor.request_status == "ok"


# ────────────────────────────────────────────────────────────────────
# T6b: 请求失败时记录 request_status=error + error_detail + 不丢已取数据
# ────────────────────────────────────────────────────────────────────

async def test_t6b_request_error_records_cursor_does_not_drop_previous(symbol_bundle_fill_two_pages):
    """第 1 次成功（1000 条），第 2 次抛异常。断言已取的 1000 条不丢 + cursor 记 error。"""
    client = _FakeClient(symbol_bundle_fill_two_pages, throw_on_call=2)
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000,
    )
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END + 10000)

    # 已取得的数据完整保留
    assert len(result.records) == 1000
    assert {f.trade_id for f in result.records} == set(range(1, 1001))

    # 游标记 error + error_detail 非空
    assert result.cursor.request_status == "error"
    assert result.cursor.error_detail is not None and "模拟网络超时" in result.cursor.error_detail
    assert result.cursor.retry_count >= 1

    # errors 列表同步
    assert len(result.errors) == 1


# ────────────────────────────────────────────────────────────────────
# T7: 脱敏 —— apiKey / signature / secretKey 被剔除；非敏感字段保留
# ────────────────────────────────────────────────────────────────────

async def test_t7_sanitize_strips_secrets_keeps_others():
    raw = {
        "id": 123,
        "symbol": "BTCUSDT",
        "time": 1700000000000,
        "apiKey": "sk-xxxxxxxxxxxxx",
        "signature": "abc123sig",
        "authorization": "Bearer token",
        "nested": {
            "secretKey": "secret-in-nested",
            "ok_field": "preserved",
        },
        "list_with_secrets": [
            {"password": "pwned", "side": "BUY"},
        ],
    }
    sanitized = sanitize_payload(raw)

    # 顶层敏感字段被剔除
    assert "apiKey" not in sanitized
    assert "signature" not in sanitized
    assert "authorization" not in sanitized
    # 保留非敏感字段
    assert sanitized["id"] == 123
    assert sanitized["symbol"] == "BTCUSDT"
    # 嵌套里 secretKey 剔除，ok_field 保留
    assert "secretKey" not in sanitized["nested"]
    assert sanitized["nested"]["ok_field"] == "preserved"
    # 列表项里的敏感字段剔除
    inner = sanitized["list_with_secrets"][0]
    assert "password" not in inner
    assert inner["side"] == "BUY"


# ────────────────────────────────────────────────────────────────────
# T8: max_pages 守卫 —— 翻页超限不崩溃，cursor 落 error_detail
# ────────────────────────────────────────────────────────────────────

async def test_t8_max_pages_guard_does_not_crash():
    """page_limit=1 时每条成交都触发翻页，max_pages=2 应强制终止且落 partial cursor。"""
    rows = [_fake_fill_row(i, _WINDOW_START + i) for i in range(100)]  # 100 条
    client = _FakeClient({_SYMBOL: rows})
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1, max_pages=2,
    )
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END + 10000)

    assert result.page_complete is False
    assert result.cursor.request_status == "partial"
    assert result.cursor.error_detail == "翻页超过 max_pages 守卫"
    assert result.errors == ["翻页超过 max_pages=2，强制终止"]


# ────────────────────────────────────────────────────────────────────
# T9: income 空窗口 —— 无收入数据时 page_complete=TRUE 不崩溃
# （整改项 3 的回归：原来 else 分支未给 cursor 赋值会 AttributeError）
# ────────────────────────────────────────────────────────────────────

async def test_t9_income_empty_window_no_crash():
    """FakeClient 的 income_records 完全空，调用应正常返回 page_complete=TRUE。"""
    client = _FakeClient({}, income_records=[])
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000,
    )
    result = await exporter.export_income(_WINDOW_START, _WINDOW_END)

    assert result.records == []
    assert result.errors == []
    assert result.page_complete is True
    assert result.cursor.request_status == "ok"
    assert result.cursor.returned_count == 0


# ────────────────────────────────────────────────────────────────────
# T10: income 非统一账户空数组告警（幻觉追问 2 的防御性测试）
# BinanceClient.get_income_history 在 use_unified_account=False 时返回 []
# 脚本无法区分"真的无收入"和"被静默拦截"，但至少不应崩溃。
# 这里 FakeClient 故意返回空列表，脚本应视为正常"无收入"收尾。
# ────────────────────────────────────────────────────────────────────

async def test_t10_income_empty_list_treated_as_normal():
    """FakeClient 直接返回空列表（模拟非统一账户被拦截或真的无收入）。"""
    client = _FakeClient({}, income_records=[])
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000,
    )
    result = await exporter.export_income(_WINDOW_START, _WINDOW_END)

    assert result.records == []
    assert result.page_complete is True
    assert result.errors == []


# ────────────────────────────────────────────────────────────────────
# 辅助：sha256 + is_sensitive_key 参数化 + _make_cursor 工厂
# ────────────────────────────────────────────────────────────────────

async def test_sha256_is_deterministic_and_sorted():
    a = {"z": 1, "a": {"b": 2}}
    b = {"a": {"b": 2}, "z": 1}
    assert compute_sha256(a) == compute_sha256(b)
    assert compute_sha256(a) == hashlib.sha256(
        b'{"a": {"b": 2}, "z": 1}'
    ).hexdigest()


@pytest.mark.parametrize("key, expected", [
    ("apiKey", True),
    ("API_KEY", True),
    ("secret", True),
    ("mySecretKey", True),
    ("signature", True),
    ("authorization", True),
    ("ok_field", False),
    ("trade_id", False),
    ("qty", False),
])
async def test_is_sensitive_key_parametrized(key, expected):
    assert is_sensitive_key(key) is expected


async def test_make_cursor_factory_produces_equivalent_record():
    """_make_cursor 工厂产出的 CursorRecord 与手写字段逐个对应一致。"""
    cursor = _make_cursor(
        source="userTrades", account_id="test", income_type=None, symbol=_SYMBOL,
        window_start_ms=1000, window_end_ms=2000,
        covered_until_ms=1500, request_status="ok",
        returned_count=42, page_complete=True,
        last_event_id=99, retry_count=0, error_detail=None,
    )
    assert cursor.source == "userTrades"
    assert cursor.symbol == _SYMBOL
    assert cursor.window_start_ms == 1000
    assert cursor.window_end_ms == 2000
    assert cursor.covered_until_ms == 1500
    assert cursor.returned_count == 42
    assert cursor.page_complete is True
    assert cursor.request_status == "ok"
    assert cursor.last_event_id == 99
    assert cursor.retry_count == 0
    assert cursor.error_detail is None


# ────────────────────────────────────────────────────────────────────
# Income 端基础覆盖
# ────────────────────────────────────────────────────────────────────

async def test_income_basic_pipeline():
    rows = [
        _fake_income_row(tran_id=i, time_ms=_WINDOW_START + i * 1000)
        for i in range(50)
    ]
    client = _FakeClient({}, income_records=rows)
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000,
    )
    result = await exporter.export_income(_WINDOW_START, _WINDOW_END)

    assert len(result.records) == 50
    assert all(isinstance(r, IncomeFact) for r in result.records)
    assert result.page_complete is True
    # 每个 fact 都有脱敏后的 raw_payload + sha256
    for r in result.records:
        assert "tranId" in r.raw_payload
        assert r.payload_sha256 == compute_sha256(r.raw_payload)


async def test_income_page_complete_false_when_full():
    """满页 1000 条时 page_complete=False，不得误标 TRUE。"""
    rows = [
        _fake_income_row(tran_id=i, time_ms=_WINDOW_START + i)
        for i in range(2500)
    ]
    client = _FakeClient({}, income_records=rows)
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000,
    )
    result = await exporter.export_income(_WINDOW_START, _WINDOW_END + 3000)

    # 2500 条分三页（1000 + 1000 + 500），最终 page_complete 为 TRUE（最后一页不满）
    assert len(result.records) == 2500
    assert result.page_complete is True
    ids = {r.tran_id for r in result.records}
    assert ids == set(range(2500))


# ────────────────────────────────────────────────────────────────────
# FillSink 被真实调用（整改项 7：原来 _FakeSink 从未被实例化）
# ────────────────────────────────────────────────────────────────────

async def test_fake_sink_upsert_fills_is_called_when_sink_provided():
    """给 exporter 注入 FakeSink，断言 upsert_fills 被调用且落的数据等于 result.records。"""
    client = _FakeClient({_SYMBOL: [_fake_fill_row(i, _WINDOW_START + i * 1000) for i in range(50)]})
    sink = _FakeSink()
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000, sink=sink,
    )
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END + 60000)

    assert result.page_complete is True
    assert len(sink.fills_upserted) == len(result.records) == 50
    assert all(isinstance(f, FillFact) for f in sink.fills_upserted)
    # save_cursor 也被调过至少一次
    assert len(sink.cursors_saved) >= 1


async def test_fake_sink_upsert_income_is_called_when_sink_provided():
    """给 exporter 注入 FakeSink，断言 upsert_income 被调用。"""
    rows = [_fake_income_row(tran_id=i, time_ms=_WINDOW_START + i * 1000) for i in range(30)]
    client = _FakeClient({}, income_records=rows)
    sink = _FakeSink()
    exporter = ExecutionFactExporter(
        account_id=_ACCOUNT, api_client=client, page_limit=1000, sink=sink,
    )
    result = await exporter.export_income(_WINDOW_START, _WINDOW_END + 60000)

    assert result.page_complete is True
    assert len(sink.income_upserted) == len(result.records) == 30
    assert all(isinstance(r, IncomeFact) for r in sink.income_upserted)
    assert len(sink.cursors_saved) >= 1
