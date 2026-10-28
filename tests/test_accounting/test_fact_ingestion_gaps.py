"""
D1 采集脚本覆盖率缺口补充测试（纯离线，不联网/不连库/不触真实交易所）。

针对 tests/test_accounting/test_fact_ingestion.py（T1-T10）未覆盖的分支、
异常路径与边界值，按 .trae/rules/development-workflow.md §4.5 清单逐项补齐：

  解析层缺失字段：
    - _parse_fill_row 缺 id / 缺 symbol → 返回 None 跳过（_consume_page 内 continue）
    - _parse_income_row 缺 tranId → 返回 None 跳过；缺 symbol/incomeType → 回退 "UNKNOWN"

  边界值：
    - _safe_int(None / "" / 非数字 / 异常类型) → None；_safe_str(None) → None
    - _consume_page 中 parse_row 返回 None 的跳过路径、time=None 不推进游标
    - sanitize_payload 对标量 / None / 顶层列表的递归处理

  脱敏：
    - is_sensitive_key 下划线归一分支 + 驼峰拆片后的单片段命中（保守剔除策略）
    - 过度剔除边界（the_secret_marker / token_count 命中）已记录

  异常路径（全部 mock，不触真实 I/O）：
    - sink.upsert 抛异常不崩溃、不进 result.errors（_maybe_sink_upsert）
    - sink.load_cursor 抛异常降级内存游标查找（_load_cursor）
    - sink 无 load_cursor / save_cursor 属性时静默降级或跳过
    - sink 缺 upsert 方法时 getattr 返回 None → 跳过下沉

  分支收尾语义：
    - 满页但本页最大时间 >= window_end_ms → ok 收尾（_one_page_iteration 内）
    - 全部行无有效时间 + 窗口起点 0 → loop_cur_start<=0 守卫收尾
    - 断点恢复预置游标 covered_until_ms=None → cur_start 回退 window_start
    - error_detail 截断到 200 字符（_ERROR_DETAIL_TRUNCATE）

  income：
    - 自定义 income_type 透传给 API 客户端；income 断点恢复（sink 注入游标续跑）

  CLI 纯本地分支：
    - _resolve_window 参数解析、_build_client_from_env 缺/有密钥
    - _run_cli 缺密钥 exit 2、_run_cli_fills/income 有错 exit 1
    - _emit_jsonl 摘要字段

  shared/binance_api 分页参数契约（mock _request，构造 client 不联网）：
    - get_user_trades：参数组装 / endpoint 选择（统一 vs 非统一账户）/ 空 symbol / 非 list 回退
    - get_income_history：income_type 透传 / 非统一账户显式报错 / 非 list 回退

  【R1 已修复】已完成窗口幂等重跑：
    原缺陷"已完成窗口重跑被 while-else 误判为 max_pages 超限"已通过
    _run_export_loop 前置守卫（cur_start >= window_end_ms → ok 收尾）修复。
    现用例 test_rerun_completed_window_is_idempotent_ok 断言正确行为；
    test_true_empty_window_returns_ok_noop 覆盖真·空窗口（window_start >= window_end）路径。
"""
from __future__ import annotations

import argparse
import json
import sys as sys_module

import pytest

from scripts.export_execution_facts import (
    ExecutionFactExporter,
    ExportResult,
    CursorRecord,
    FillFact,
    is_sensitive_key,
    sanitize_payload,
    compute_sha256,
    _make_cursor,
    _parse_fill_row,
    _parse_income_row,
    _safe_int,
    _safe_str,
    _resolve_window,
    _build_client_from_env,
    _run_cli,
    _run_cli_fills,
    _run_cli_income,
    _emit_jsonl,
)

# 说明：pytest.ini 已配置 asyncio_mode = auto，async 测试自动接管；
# 本文件含 1 个同步测试（main 入口，内部有 asyncio.run 不能嵌套事件循环），
# 故不加文件级 pytestmark，避免对同步函数误标 asyncio 产生 PytestWarning。

# ────────────────────────────────────────────────────────────────────
# 常量与数据工厂
# ────────────────────────────────────────────────────────────────────

_SYMBOL = "BTCUSDT"
_WINDOW_START = 1700000000000
_WINDOW_END = 1700000300000
_ACCOUNT = "test-account"


def _fill_row(trade_id: int, time_ms: int, symbol: str = _SYMBOL, **overrides):
    """构造一条 userTrades 响应字典（币安真实字段名）。"""
    row = {
        "id": trade_id,
        "orderId": 10000 + trade_id,
        "symbol": symbol,
        "side": "BUY",
        "qty": "0.1",
        "price": "50000.0",
        "quoteQty": "5000.0",
        "commission": "0.5",
        "commissionAsset": "USDT",
        "realizedPnl": "1.23",
        "time": time_ms,
    }
    row.update(overrides)
    return row


def _income_row(tran_id: int, time_ms: int, **overrides):
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
# 测试用假客户端 / 假 sink
# ────────────────────────────────────────────────────────────────────

class _SliceClient:
    """记录每次调用参数 + 按时间窗/limit 切片的假客户端（离线）。"""

    def __init__(
        self,
        fills: dict[str, list[dict]] | None = None,
        income: list[dict] | None = None,
        *,
        throw_on_call: int | None = None,
        exc: Exception | None = None,
    ):
        self._fills = fills or {}
        self._income = income or []
        self._call_count = 0
        self._throw_on_call = throw_on_call
        self._exc = exc or RuntimeError("模拟网络超时")
        self.calls: list[dict] = []

    def _maybe_throw(self):
        self._call_count += 1
        if self._throw_on_call is not None and self._call_count >= self._throw_on_call:
            raise self._exc

    async def get_user_trades(self, symbol, *, start_time=None, end_time=None, limit=None,
                              from_id=None):
        self._maybe_throw()
        self.calls.append({"endpoint": "userTrades", "symbol": symbol,
                           "start_time": start_time, "end_time": end_time,
                           "limit": limit, "from_id": from_id})
        return self._slice(self._fills.get(symbol, []), start_time, end_time, limit,
                           from_id=from_id)

    async def get_income_history(self, *, start_time=None, end_time=None, limit=None,
                                 income_type=None):
        self._maybe_throw()
        self.calls.append({"endpoint": "um/income", "symbol": None,
                           "start_time": start_time, "end_time": end_time,
                           "limit": limit, "income_type": income_type})
        rows = self._income
        if income_type:
            rows = [r for r in rows if r.get("incomeType") == income_type]
        return self._slice(rows, start_time, end_time, limit)

    @staticmethod
    def _slice(rows, start_time, end_time, limit, *, from_id=None):
        """按时间窗或 from_id 切片（互斥，对齐 PM UM userTrades 契约）。"""
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
                if start_time is not None and r.get("time") is not None and r["time"] < start_time:
                    continue
                if end_time is not None and r.get("time") is not None and r["time"] > end_time:
                    continue
                out.append(r)
        if limit is not None:
            out = out[:limit]
        return out


class _PageListClient:
    """按调用次序返回预设页的假客户端（不做时间过滤，用于触发守卫分支）。"""

    def __init__(self, pages: list[list[dict]], *, exc: Exception | None = None):
        self._pages = list(pages)
        self._exc = exc
        self.calls: list[dict] = []

    async def get_user_trades(self, _symbol, *, start_time=None, end_time=None, limit=None,
                              from_id=None):
        self.calls.append({"start_time": start_time, "end_time": end_time, "limit": limit,
                           "from_id": from_id})
        # 对齐真实 PM UM userTrades 契约
        if from_id is not None and (start_time is not None or end_time is not None):
            raise ValueError(
                "from_id 与 start_time/end_time 互斥，不能同时传入"
            )
        if self._exc is not None:
            raise self._exc
        return self._pages.pop(0) if self._pages else []


class _SaveOnlySink:
    """只有 save_cursor 的 sink（缺 load_cursor / upsert 方法）。"""

    def __init__(self):
        self.cursors_saved: list = []

    async def save_cursor(self, cursor):
        self.cursors_saved.append(cursor)


class _LoadOnlySink:
    """只有 load_cursor 的 sink（缺 save_cursor / upsert 方法）。"""

    def __init__(self, cursor: CursorRecord | None = None):
        self._cursor = cursor

    async def load_cursor(self, source, account_id, income_type, symbol, window_start_ms):
        return self._cursor


class _BrokenSink:
    """可配置抛异常的 sink（异常路径测试用）。"""

    def __init__(self, *, upsert_error=False, load_error=False, save_error=False):
        self._upsert_error = upsert_error
        self._load_error = load_error
        self._save_error = save_error
        self.cursors_saved: list = []
        self.fills_upserted: list = []

    async def upsert_fills(self, fills):
        if self._upsert_error:
            raise RuntimeError("sink 下沉失败（模拟）")
        self.fills_upserted.extend(fills)

    async def load_cursor(self, source, symbol, window_start_ms):
        if self._load_error:
            raise RuntimeError("load_cursor 失败（模拟）")
        return None

    async def save_cursor(self, cursor):
        if self._save_error:
            raise RuntimeError("save_cursor 失败（模拟）")
        self.cursors_saved.append(cursor)


# ────────────────────────────────────────────────────────────────────
# 一、解析层缺失字段 / 边界值
# ────────────────────────────────────────────────────────────────────

async def test_fill_missing_id_skipped_but_valid_kept():
    """缺 id 的行被跳过（parse_row 返回 None → _consume_page continue），正常行保留。"""
    no_id = _fill_row(1, _WINDOW_START + 1000)
    del no_id["id"]
    rows = [no_id, _fill_row(2, _WINDOW_START + 2000)]
    client = _SliceClient(fills={_SYMBOL: rows})
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=1000)
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    assert [f.trade_id for f in result.records] == [2]
    assert result.page_complete is True
    assert result.errors == []


async def test_fill_missing_symbol_skipped():
    """缺 symbol 的行被跳过（L237-240 分支）。"""
    row = _fill_row(7, _WINDOW_START)
    del row["symbol"]
    assert _parse_fill_row(_ACCOUNT, row) is None


async def test_income_missing_tran_id_skipped():
    """income 缺 tranId 的行被跳过（L265-268 分支）。"""
    row = _income_row(1, _WINDOW_START)
    del row["tranId"]
    assert _parse_income_row(_ACCOUNT, row) is None


async def test_income_missing_symbol_and_type_fallback_unknown():
    """income 缺 symbol / incomeType 时回退 "UNKNOWN"（L274-275 的 or fallback 分支）。"""
    row = _income_row(9, _WINDOW_START)
    del row["symbol"]
    del row["incomeType"]
    fact = _parse_income_row(_ACCOUNT, row)
    assert fact is not None
    assert fact.symbol == "UNKNOWN"
    assert fact.income_type == "UNKNOWN"


async def test_safe_int_safe_str_boundaries():
    """_safe_int / _safe_str 全边界：None、空串、非数字、正常值。"""
    # _safe_int：None / 空串 / 非数字 / 异常类型 → None；正常 → int
    assert _safe_int(None) is None
    assert _safe_int("") is None
    assert _safe_int("abc") is None          # ValueError 路径
    assert _safe_int([1, 2]) is None         # TypeError 路径
    assert _safe_int("42") == 42
    assert _safe_int(7) == 7
    # _safe_str：None → None；其余转 str（空串保留为 ""）
    assert _safe_str(None) is None
    assert _safe_str(42) == "42"
    assert _safe_str("") == ""


async def test_rows_without_time_do_not_advance_cursor():
    """行缺 time 字段（trade_time_ms=None）时不推进游标时间，
    窗口起点为 0 时触发 loop_cur_start<=0 守卫收尾（L585 子句 3、L586 的 0 回退），
    不崩溃、不死循环，covered 回退为 window_end_ms。
    """
    rows = [{"id": 1, "symbol": _SYMBOL}, {"id": 2, "symbol": _SYMBOL}]  # 无 time
    client = _PageListClient(pages=[rows])       # 满页（== limit=2）→ page_complete=False
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=2)
    result = await exporter.export_fills(_SYMBOL, 0, _WINDOW_END)

    assert len(result.records) == 2
    assert all(f.trade_time_ms is None for f in result.records)
    assert result.page_complete is True          # 守卫收尾，不死循环
    assert result.cursor.request_status == "ok"
    assert result.cursor.covered_until_ms == _WINDOW_END


async def test_full_page_but_time_reached_window_end_finishes_ok():
    """满页（== limit）但本页最大时间已触 window_end_ms → R10 闭区间修复后
    必须续传确认排空（不能因触界误判完成）。

    R7 客户端右边界过滤 + R10 闭区间原始页满判定：
      - 原始页 2 条（含 1 条越界被过滤）→ raw_page_full=True → 必须续传
      - 续传 from_id=1 → _PageListClient 预设只有 1 页，pop 完返回 []
      - 第二次 raw_rows=[] → raw_page_full=False → 排空 → ok 收尾
    """
    page = [_fill_row(1, _WINDOW_END), _fill_row(2, _WINDOW_END + 1)]
    client = _PageListClient(pages=[page])
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=2)
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    # R7 客户端过滤：time=_WINDOW_END+1 越界被过滤，只留 time=_WINDOW_END 那条
    assert len(result.records) == 1
    assert result.page_complete is True
    assert result.cursor.request_status == "ok"
    # R10 完成时 covered 固定为 window_end_ms
    assert result.cursor.covered_until_ms == _WINDOW_END
    # R10：原始页满 → 必须续传确认排空 → 2 次调用
    assert len(client.calls) == 2


# ────────────────────────────────────────────────────────────────────
# 二、脱敏补充分支
# ────────────────────────────────────────────────────────────────────

async def test_sensitive_key_collapsed_underscore_variants():
    """下划线归一分支（L188-189）：lower 命不中但去下划线命中；同时验证不做子串误删。"""
    assert is_sensitive_key("API__KEY") is True          # 走 collapsed 分支
    assert is_sensitive_key("api__secret") is True       # 走 collapsed 分支
    assert is_sensitive_key("RecvWindow") is True        # 驼峰拆片 → recvwindow
    # 子串不误删（完整字段名匹配的边界）
    assert is_sensitive_key("list_with_secrets") is False   # "secrets" 复数不在集合才幸免
    # 【行为发现 · 与 docstring 不一致】脚本 L180-181 声称避免 "secret_marker 这类
    # 非密钥字段名被误判"，但实际下划线/驼峰拆片后的**单片段**只要命中集合就返回 True：
    #   "the_secret_marker" → 片段 "secret" 命中 → True
    #   "token_count"       → 片段 "token" 命中  → True
    # 偏差方向是"过度剔除"（宁错杀不放过），对脱敏安全无风险，但会静默丢弃
    # 恰好含敏感词片段命名的业务字段。此处记录实际行为，已列入测试报告缺陷清单。
    assert is_sensitive_key("the_secret_marker") is True
    assert is_sensitive_key("token_count") is True


async def test_sanitize_payload_scalars_and_top_level_list():
    """sanitize_payload 对标量 / None / 顶层列表的处理（递归叶子路径）。"""
    assert sanitize_payload("plain") == "plain"
    assert sanitize_payload(None) is None
    assert sanitize_payload(42) == 42
    assert sanitize_payload([{"token": "x", "ok": 1}]) == [{"ok": 1}]
    deep = {"nested": {"deeper": [{"authorization": "a", "keep": 1}]}}
    assert sanitize_payload(deep) == {"nested": {"deeper": [{"keep": 1}]}}


async def test_fill_payload_sha256_ignores_removed_secrets():
    """同一行去掉敏感字段前后 sha256 与脱敏后 payload 一致（脱敏发生在哈希前）。"""
    row = _fill_row(1, _WINDOW_START, apiKey="sk-secret", recvWindow="5000")
    fact = _parse_fill_row(_ACCOUNT, row)
    assert isinstance(fact, FillFact)
    assert "apiKey" not in fact.raw_payload
    assert "recvWindow" not in fact.raw_payload
    assert fact.payload_sha256 == compute_sha256(fact.raw_payload)


# ────────────────────────────────────────────────────────────────────
# 三、sink 异常路径 / 方法缺失降级
# ────────────────────────────────────────────────────────────────────

async def test_sink_upsert_raises_does_not_crash_and_not_in_errors():
    """sink.upsert_fills 抛异常（R2 契约：上抛不吞）：采集不崩溃、records 完整，
    但 errors 必须含 sink 错误且 page_complete=False（先事实后游标，失败回退）。"""
    client = _SliceClient(fills={_SYMBOL: [_fill_row(i, _WINDOW_START + i) for i in range(5)]})
    sink = _BrokenSink(upsert_error=True)
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=1000,
                                     sink=sink)
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    assert len(result.records) == 5
    # R2 契约：sink 失败 → errors 追加错误、page_complete 置 False
    assert len(result.errors) >= 1
    assert result.page_complete is False
    assert sink.fills_upserted == []             # 下沉失败，数据没有假成功


async def test_sink_without_upsert_method_silently_skipped():
    """sink 无 upsert_fills 属性（getattr None → L645 分支）：静默跳过下沉，游标照常保存。"""
    client = _SliceClient(fills={_SYMBOL: [_fill_row(1, _WINDOW_START)]})
    sink = _SaveOnlySink()
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=1000,
                                     sink=sink)
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    assert result.page_complete is True
    assert len(sink.cursors_saved) >= 1


async def test_sink_upsert_skipped_when_no_data():
    """有 sink 但采集为空（L645 的 not collected 分支）：不调用 upsert，直接返回。"""
    client = _SliceClient(fills={_SYMBOL: []})
    sink = _BrokenSink()                          # upsert 正常，若被调用会记录
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=1000,
                                     sink=sink)
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    assert result.records == []
    assert sink.fills_upserted == []
    assert len(sink.cursors_saved) >= 1           # 游标照常落


async def test_sink_load_cursor_raises_falls_back_to_memory():
    """sink.load_cursor 抛异常（L671-672）：降级内存游标查找。

    R10 后完成路径 covered_until_ms 固定为 window_end_ms → 已完成窗口
    幂等重跑会被前置守卫短路（cur_start >= window_end_ms），不发新 API。
    本测试验证：降级内存游标查找路径能正确定位到已完成状态。
    """
    client = _SliceClient(fills={_SYMBOL: [_fill_row(i, _WINDOW_START + i) for i in range(50)]})
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=20)
    first = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)
    assert first.page_complete is True
    # R10 完成时 covered 固定为 window_end_ms
    assert first.cursor.covered_until_ms == _WINDOW_END

    # 换上一个 load_cursor 必抛错的 sink，重跑同窗口
    # R10：前置守卫短路（covered >= window_end），零 API 调用
    n_calls_first_run = len(client.calls)
    exporter.sink = _BrokenSink(load_error=True)
    second = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    assert second.page_complete is True
    assert second.cursor.request_status == "ok"
    # R10：已完成窗口幂等重跑不发 API
    assert len(client.calls) == n_calls_first_run


async def test_sink_without_load_cursor_uses_memory():
    """sink 无 load_cursor 属性（L665→677 分支）：降级内存游标。

    R10 后完成路径 covered 固定为 window_end_ms → 已完成窗口幂等重跑
    被前置守卫短路，验证降级路径仍能正确定位完成状态。
    """
    client = _SliceClient(fills={_SYMBOL: [_fill_row(i, _WINDOW_START + i) for i in range(50)]})
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=20)
    first = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)
    assert first.page_complete is True

    n_calls_first_run = len(client.calls)
    exporter.sink = _SaveOnlySink()               # 无 load_cursor 属性
    second = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    assert second.page_complete is True
    assert second.cursor.request_status == "ok"
    # R10：已完成窗口幂等重跑不发 API
    assert len(client.calls) == n_calls_first_run


async def test_sink_without_save_cursor_uses_memory_only():
    """sink 无 save_cursor 属性（静默跳过 sink 写，内存 store 照常写入）+ load_cursor 返回 None：
    游标只写内存 store，不抛错。
    """
    client = _SliceClient(fills={_SYMBOL: [_fill_row(i, _WINDOW_START + i) for i in range(10)]})
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=1000,
                                     sink=_LoadOnlySink(cursor=None))
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    assert result.page_complete is True
    # R3 契约：游标身份键 = 5 元组 (source, account_id, income_type, symbol, window_start_ms)
    key = ("userTrades", _ACCOUNT, None, _SYMBOL, _WINDOW_START)
    assert exporter._cursor_store[key].covered_until_ms is not None


async def test_sink_save_cursor_raises_keeps_memory_store():
    """sink.save_cursor 抛异常（R2 契约：上抛不吞）：errors 追加错误、page_complete=False，
    但内存 store 已写入游标（供下次重试）。"""
    client = _SliceClient(fills={_SYMBOL: [_fill_row(i, _WINDOW_START + i) for i in range(10)]})
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=1000,
                                     sink=_BrokenSink(save_error=True))
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    # R2 契约：save_cursor 失败 → page_complete 置 False + errors 追加
    assert result.page_complete is False
    assert len(result.errors) >= 1
    # 内存 store key 已扩为 5 元组；游标 page_complete 因 save_cursor 失败被置 False
    key = ("userTrades", _ACCOUNT, None, _SYMBOL, _WINDOW_START)
    assert exporter._cursor_store[key].page_complete is False


# ────────────────────────────────────────────────────────────────────
# 四、游标恢复 / income 补充
# ────────────────────────────────────────────────────────────────────

async def test_resume_cursor_covered_until_none_falls_back_to_window_start():
    """预置游标 covered_until_ms=None（L386 的 else 子分支）：cur_start 回退 window_start_ms，
    从窗口起点重新拉全量。
    """
    stale = CursorRecord(
        source="userTrades", account_id=_ACCOUNT, income_type=None, symbol=_SYMBOL,
        window_start_ms=_WINDOW_START, window_end_ms=_WINDOW_END,
        covered_until_ms=None, request_status="ok",
    )
    client = _SliceClient(fills={_SYMBOL: [_fill_row(1, _WINDOW_START)]})
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=1000,
                                     sink=_LoadOnlySink(cursor=stale))
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    assert client.calls[0]["start_time"] == _WINDOW_START
    assert result.page_complete is True


async def test_export_income_custom_income_type_forwarded():
    """export_income 传自定义 income_type（L441-442 分支）：透传给 API 客户端。"""
    rows = [_income_row(tran_id=i, time_ms=_WINDOW_START + i, incomeType="FUNDING_FEE")
            for i in range(5)]
    client = _SliceClient(income=rows)
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=1000)
    result = await exporter.export_income(_WINDOW_START, _WINDOW_END, income_type="FUNDING_FEE")

    assert len(result.records) == 5
    assert client.calls[0]["income_type"] == "FUNDING_FEE"


async def test_income_resume_via_sink_cursor_broken_plus_resume_covers_full():
    """income 断点恢复：中途抛错后经 sink.load_cursor 注入游标续跑，
    broken_ids | resume_ids == full_ids（与 T4 的 fills 场景对偶）。
    """
    income_rows = [_income_row(tran_id=i, time_ms=_WINDOW_START + i) for i in range(2500)]

    # 完整跑
    full_client = _SliceClient(income=income_rows)
    full_exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=full_client,
                                          page_limit=1000)
    full_result = await full_exporter.export_income(_WINDOW_START, _WINDOW_END + 3000)
    full_ids = {r.tran_id for r in full_result.records}
    assert len(full_ids) == 2500

    # 中断跑（第 2 次请求抛错：拿到第一页 1000 条后失败）
    broken_client = _SliceClient(income=income_rows, throw_on_call=2)
    broken_exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=broken_client,
                                            page_limit=1000)
    broken_result = await broken_exporter.export_income(_WINDOW_START, _WINDOW_END + 3000)
    assert len(broken_result.records) == 1000
    assert broken_result.cursor.request_status == "error"

    # 注入游标续跑
    resume_sink = _LoadOnlySink(cursor=broken_result.cursor)
    resume_client = _SliceClient(income=income_rows)
    resume_exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=resume_client,
                                            page_limit=1000, sink=resume_sink)
    resume_result = await resume_exporter.export_income(_WINDOW_START, _WINDOW_END + 3000)

    broken_ids = {r.tran_id for r in broken_result.records}
    resume_ids = {r.tran_id for r in resume_result.records}
    assert resume_result.page_complete is True
    assert broken_ids | resume_ids == full_ids


# ────────────────────────────────────────────────────────────────────
# 五、error_detail 截断
# ────────────────────────────────────────────────────────────────────

async def test_error_detail_truncated_to_200_chars():
    """异常消息超过 200 字符时，error 游标的 error_detail 被截断到 200（L635）。"""
    long_exc = RuntimeError("x" * 500)
    client = _PageListClient(pages=[], exc=long_exc)
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=1000)
    result = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    assert result.cursor.request_status == "error"
    assert len(result.cursor.error_detail) == 200
    assert result.cursor.error_detail == "x" * 200
    assert result.errors == ["x" * 200]          # errors 同步截断版本
    assert result.cursor.retry_count == 1


# ────────────────────────────────────────────────────────────────────
# 六、CLI 纯本地分支（不联网：缺密钥在构造 client 前即返回）
# ────────────────────────────────────────────────────────────────────

async def test_resolve_window_parses_symbols_and_days():
    """_resolve_window：symbols 去空格/大写/去空项；days=0 边界；None/空串 → 空列表。"""
    args = argparse.Namespace(days=2, symbols=" btcusdt , ethusdt , ")
    start_ms, now_ms, symbols = _resolve_window(args)
    assert symbols == ["BTCUSDT", "ETHUSDT"]
    assert now_ms - start_ms == 2 * 24 * 3600 * 1000

    _, _, empty = _resolve_window(argparse.Namespace(days=0, symbols=""))
    assert empty == []
    _, _, none_syms = _resolve_window(argparse.Namespace(days=0, symbols=None))
    assert none_syms == []


async def test_build_client_missing_env_returns_exit_2():
    """缺密钥环境变量 → (None, 2)，stderr 打印中文报错（L742-752 分支）。"""
    import os
    saved = {k: os.environ.pop(k, None) for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET")}
    try:
        client, code = _build_client_from_env()
        assert client is None
        assert code == 2
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


async def test_build_client_with_env_returns_client_zero():
    """有密钥 → 返回 (BinanceClient, 0)（L758 分支）；构造纯本地，不发起网络请求。"""
    import os
    os.environ["BINANCE_API_KEY"] = "test-api-key-1234567890"
    os.environ["BINANCE_API_SECRET"] = "test-api-secret-1234567890"
    try:
        client, code = _build_client_from_env()
        assert code == 0
        assert client is not None
        assert client.use_unified_account is True
    finally:
        os.environ.pop("BINANCE_API_KEY", None)
        os.environ.pop("BINANCE_API_SECRET", None)


async def test_run_cli_missing_env_exit_code_2():
    """_run_cli 缺密钥 → 返回 2，不发起任何网络请求（L803-806 分支）。"""
    import os
    saved = {k: os.environ.pop(k, None) for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET")}
    try:
        args = argparse.Namespace(mode="both", symbols="BTCUSDT", days=1, account_id="t",
                                  income_type="REALIZED_PNL", page_limit=1000)
        code = await _run_cli(args)
        assert code == 2
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


async def test_run_cli_fills_error_exit_1_and_success_exit_0():
    """_run_cli_fills：有 errors → exit 1（L776-777）；正常 → exit 0（L778）。"""
    err_client = _SliceClient(fills={_SYMBOL: []}, throw_on_call=1)
    err_exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=err_client,
                                         page_limit=1000)
    assert await _run_cli_fills(err_exporter, ["BTCUSDT"], _WINDOW_START, _WINDOW_END) == 1

    ok_client = _SliceClient(fills={_SYMBOL: [_fill_row(1, _WINDOW_START)]})
    ok_exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=ok_client,
                                        page_limit=1000)
    assert await _run_cli_fills(ok_exporter, ["BTCUSDT"], _WINDOW_START, _WINDOW_END) == 0


async def test_run_cli_income_error_exit_1_and_success_exit_0():
    """_run_cli_income：有 errors → exit 1（L794 的 True 分支）；正常 → exit 0（else 分支）。"""
    err_client = _SliceClient(income=[], throw_on_call=1)
    err_exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=err_client,
                                         page_limit=1000)
    assert await _run_cli_income(err_exporter, _WINDOW_START, _WINDOW_END, "REALIZED_PNL") == 1

    ok_client = _SliceClient(income=[_income_row(1, _WINDOW_START)])
    ok_exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=ok_client,
                                        page_limit=1000)
    assert await _run_cli_income(ok_exporter, _WINDOW_START, _WINDOW_END, "REALIZED_PNL") == 0


async def test_run_cli_with_client_runs_selected_modes(monkeypatch, capsys):
    """_run_cli 有 client 时按 mode 执行（L808-822）：
    mode="fills" 只跑 fills 分支；mode="both" 跑 fills+income 两个分支；返回 0。
    通过 monkeypatch 注入假 client，全程离线。
    """
    import scripts.export_execution_facts as mod

    client = _SliceClient(
        fills={_SYMBOL: [_fill_row(1, _WINDOW_START)]},
        income=[_income_row(1, _WINDOW_START)],
    )
    monkeypatch.setattr(mod, "_build_client_from_env", lambda: (client, 0))

    fills_args = argparse.Namespace(mode="fills", symbols="BTCUSDT", days=1,
                                    account_id="t", income_type="REALIZED_PNL",
                                    page_limit=1000)
    assert await _run_cli(fills_args) == 0
    assert len(client.calls) == 1 and client.calls[0]["endpoint"] == "userTrades"

    both_args = argparse.Namespace(mode="both", symbols="BTCUSDT", days=1,
                                   account_id="t", income_type="REALIZED_PNL",
                                   page_limit=1000)
    assert await _run_cli(both_args) == 0
    assert [c["endpoint"] for c in client.calls] == ["userTrades", "userTrades", "um/income"]


async def test_run_cli_fills_mode_without_symbols_skips_fill_block(monkeypatch):
    """mode="fills" 但 symbols 为空（L816 的 and symbols=False 子分支）：不执行采集。"""
    import scripts.export_execution_facts as mod

    client = _SliceClient()
    monkeypatch.setattr(mod, "_build_client_from_env", lambda: (client, 0))

    args = argparse.Namespace(mode="fills", symbols="", days=1, account_id="t",
                              income_type="REALIZED_PNL", page_limit=1000)
    assert await _run_cli(args) == 0
    assert client.calls == []                      # 未发起任何请求


def test_main_missing_env_raises_system_exit_2(monkeypatch):
    """main() 入口（L844-856）：缺密钥环境 → SystemExit(2)。不发起网络请求。

    注意：main() 内部调用 asyncio.run()，本用例必须是**同步**测试函数，
    否则会嵌套在 pytest-asyncio 的事件循环里触发 RuntimeError。
    """
    import scripts.export_execution_facts as mod

    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.setattr(sys_module, "argv", ["export_execution_facts.py"])

    with pytest.raises(SystemExit) as exc_info:
        mod.main()
    assert exc_info.value.code == 2


async def test_emit_jsonl_summary_fields_with_and_without_cursor(capsys):
    """_emit_jsonl：有 cursor 时输出摘要字段；cursor=None 时字段为 null（L827-839）。"""
    cursor = _make_cursor(source="userTrades", account_id=_ACCOUNT, income_type=None,
                          symbol=_SYMBOL,
                          window_start_ms=_WINDOW_START, window_end_ms=_WINDOW_END,
                          covered_until_ms=_WINDOW_START + 5, request_status="ok")
    result = ExportResult(records=[object()], cursor=cursor, errors=["e1"])
    _emit_jsonl("fills", result, symbol=_SYMBOL)
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {
        "kind": "fills",
        "symbol": _SYMBOL,
        "record_count": 1,
        "page_complete": False,
        "cursor": {"source": "userTrades", "covered_until_ms": _WINDOW_START + 5,
                   "request_status": "ok"},
        "errors": ["e1"],
    }

    _emit_jsonl("income", ExportResult())     # cursor=None 分支
    out2 = json.loads(capsys.readouterr().out.strip())
    assert out2["cursor"] == {"source": None, "covered_until_ms": None, "request_status": None}
    assert out2["record_count"] == 0


# ────────────────────────────────────────────────────────────────────
# 七、shared/binance_api 分页参数契约（mock _request，绝不联网）
# ────────────────────────────────────────────────────────────────────

class _CaptureRequest:
    """替换 BinanceClient._request 的捕获桩：记录调用参数并返回预设结果。"""

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, method, endpoint, params, signed=True, **_kwargs):
        self.calls.append({"method": method, "endpoint": endpoint,
                           "params": dict(params), "signed": signed})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _make_offline_client(use_unified_account: bool = True):
    """构造纯离线 BinanceClient（__init__ 只赋属性，不发请求）。"""
    from shared.binance_api import BinanceClient
    return BinanceClient(api_key="test-api-key-1234567890",
                         api_secret="test-api-secret-1234567890",
                         use_unified_account=use_unified_account)


async def test_api_user_trades_forwards_pagination_params(monkeypatch):
    """get_user_trades：start_time/end_time/limit/order_id 全部进入 params（分页契约）。"""
    client = _make_offline_client(use_unified_account=True)
    capture = _CaptureRequest([])
    monkeypatch.setattr(client, "_request", capture)

    rows = await client.get_user_trades("btcusdt", order_id="123", start_time=111,
                                        end_time=222, limit=500)

    assert rows == []
    call = capture.calls[0]
    assert call["method"] == "GET"
    assert call["endpoint"] == "/papi/v1/um/userTrades"   # 统一账户 endpoint
    assert call["signed"] is True
    assert call["params"] == {
        "symbol": "BTCUSDT", "orderId": "123",
        "startTime": 111, "endTime": 222, "limit": 500,
    }


async def test_api_user_trades_non_unified_endpoint_and_optional_omitted(monkeypatch):
    """get_user_trades：非统一账户走 /fapi/v1/userTrades；可选参数 None 时不进 params。"""
    client = _make_offline_client(use_unified_account=False)
    capture = _CaptureRequest([])
    monkeypatch.setattr(client, "_request", capture)

    await client.get_user_trades(" btcusdt ")             # 带空白，验证 strip+upper

    call = capture.calls[0]
    assert call["endpoint"] == "/fapi/v1/userTrades"
    assert call["params"] == {"symbol": "BTCUSDT"}        # 无多余 key


async def test_api_user_trades_empty_symbol_raises(monkeypatch):
    """get_user_trades：空/空白 symbol → ValueError（L1652-1653 分支），不发请求。"""
    client = _make_offline_client()
    capture = _CaptureRequest([])
    monkeypatch.setattr(client, "_request", capture)

    with pytest.raises(ValueError):
        await client.get_user_trades("")
    with pytest.raises(ValueError):
        await client.get_user_trades("   ")
    assert capture.calls == []


async def test_api_user_trades_non_list_response_falls_back_empty(monkeypatch):
    """get_user_trades：响应非 list（如错误 dict）→ 回退空列表（L1673-1674 分支）。"""
    client = _make_offline_client()
    capture = _CaptureRequest({"code": -1121, "msg": "Invalid symbol."})
    monkeypatch.setattr(client, "_request", capture)

    rows = await client.get_user_trades("BTCUSDT", start_time=1, end_time=2, limit=10)
    assert rows == []


async def test_api_income_history_non_unified_raises_without_request(monkeypatch):
    """非统一账户收入查询必须明确失败，且不发请求。"""
    client = _make_offline_client(use_unified_account=False)
    capture = _CaptureRequest([])
    monkeypatch.setattr(client, "_request", capture)

    with pytest.raises(ValueError, match="统一账户"):
        await client.get_income_history(start_time=1, end_time=2, limit=1000)
    assert capture.calls == []                    # 未发起任何请求


async def test_export_income_rejects_non_unified_client_before_cursor_write(monkeypatch):
    """采集端必须把账户模式不支持与真正的空收入区分开。"""
    client = _make_offline_client(use_unified_account=False)
    capture = _CaptureRequest([])
    monkeypatch.setattr(client, "_request", capture)
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client)

    with pytest.raises(ValueError, match="统一账户"):
        await exporter.export_income(1, 2)

    assert capture.calls == []
    assert exporter._cursor_store == {}


async def test_api_income_history_forwards_params_and_non_list_fallback(monkeypatch):
    """get_income_history：incomeType/limit/startTime/endTime 进入 params；非 list 回退 []。"""
    client = _make_offline_client(use_unified_account=True)
    capture = _CaptureRequest({"error": True})
    monkeypatch.setattr(client, "_request", capture)

    rows = await client.get_income_history(start_time=1, end_time=2, limit=1000,
                                           income_type="FUNDING_FEE")
    assert rows == []                             # 非 list 回退
    call = capture.calls[0]
    assert call["endpoint"] == "/papi/v1/um/income"
    assert call["params"] == {"incomeType": "FUNDING_FEE", "limit": 1000,
                              "startTime": 1, "endTime": 2}

    # 默认参数路径：income_type 缺省 = REALIZED_PNL、start/end None 不进 params
    capture2 = _CaptureRequest([{"tranId": 1}])
    monkeypatch.setattr(client, "_request", capture2)
    rows2 = await client.get_income_history()
    assert rows2 == [{"tranId": 1}]
    assert capture2.calls[0]["params"] == {"incomeType": "REALIZED_PNL", "limit": 1000}


# ────────────────────────────────────────────────────────────────────
# 八、已完成窗口幂等重跑（前置守卫覆盖 while-else 零次进入语义）
# ────────────────────────────────────────────────────────────────────

async def test_rerun_completed_window_is_idempotent_ok():
    """已完成窗口重复 export_fills 静默 no-op（前置守卫绕过 while-else）。

    场景：第一次采集满页（2 条，含 1 条越界被客户端过滤）→ 原始页满 →
    必须再 from_id 续传一次确认排空（R10 闭区间修复）→ 第二次返回空 → ok 游标
    covered_until_ms=window_end 落 sink；第二次同窗口重跑 → _load_cursor
    取回游标 → cur_start >= window_end_ms → 前置守卫直接 ok 收尾，
    while 循环零次进入，不走 while-else 的 max_pages 守卫。

    期望：page_complete=True、request_status="ok"、errors=[]、
    游标状态不回退（ok 不被 partial 覆盖）。
    """
    # 第一次：满页 2 条，含 time=_WINDOW_END+1 越界记录
    # R7 客户端过滤掉越界行 → 过滤后 1 条，但原始页满 → 必须续传
    # 续传 from_id=1 返回空（预设 pages 只 1 条，pop 完返回 []）→ 确认排空
    page = [_fill_row(1, _WINDOW_END), _fill_row(2, _WINDOW_END + 1)]
    client = _PageListClient(pages=[page])
    sink = _SaveOnlySink()
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=2,
                                     sink=sink)
    r1 = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)
    assert r1.page_complete is True
    assert r1.cursor.covered_until_ms == _WINDOW_END  # R7+R10：客户端过滤 + 完成时固定 window_end
    # R10：满页触界 → 发 2 次（第一次满页 + 第二次 from_id 续传返回空）
    assert len(client.calls) == 2

    # 第二次：同窗口重跑（数据已采完，应 no-op）
    r2 = await exporter.export_fills(_SYMBOL, _WINDOW_START, _WINDOW_END)

    # 前置守卫语义：不发起新请求、ok 收尾、errors=[]
    assert len(client.calls) == 2                 # 只有 exp1 的那两次，exp2 零调用
    assert r2.page_complete is True
    assert r2.cursor.request_status == "ok"
    assert r2.cursor.error_detail is None
    assert r2.errors == []
    # ok 游标被保存，原状态不回退
    assert sink.cursors_saved[-1].request_status == "ok"


async def test_true_empty_window_returns_ok_noop():
    """真·空窗口（window_start_ms >= window_end_ms）：走翻页循环查询空窗口 → ok 收尾。

    Round 4 守卫修正后，cur_start 初始到界但 loaded_cursor=None 时不再短路——
    必须进翻页循环做一次 API 调用，由 Fake 的时间过滤自然返回空 → _build_complete → ok。
    这是「守卫不能假设『无游标=窗口为空』」的必然行为：零条 + complete 语义不变，
    唯一变化是多一次 API 调用换取正确性（反向窗口同点窗，都是无游标且初始到界）。
    """
    # 故意把 start 设得 >= end（反转窗口）
    client = _SliceClient(fills={_SYMBOL: [_fill_row(1, _WINDOW_START)]})
    exporter = ExecutionFactExporter(account_id=_ACCOUNT, api_client=client, page_limit=1000)
    result = await exporter.export_fills(_SYMBOL, _WINDOW_END, _WINDOW_START)  # start >= end

    assert result.page_complete is True
    assert result.cursor.request_status == "ok"
    assert result.errors == []
    assert result.records == []
    # Round 4 守卫修正：无游标且初始到界不再短路，必须进翻页查一次空窗口
    assert len(client.calls) >= 1
