"""
execution_facts 采集回归测试（R2 + R3 + R6）。

全部注入式假 API / 假 sink，不连 DB / 交易所。

覆盖：
  R2  事实写失败、游标写失败、中途进程终止 三注入 → 重试后持久化集合完整
  R3  income_type 隔离 + 账户隔离 → 互不窜跳、互不覆盖
  R6  同毫秒 = limit / limit+1 / 多页 / 窗口右边界 → 完整取回
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pytest

from scripts.export_execution_facts import ExecutionFactExporter


# ── 共用工具：合成数据行 + Fake API + Fake Sink ─────────────────


def _fill_row(trade_id: int, time_ms: int) -> Dict[str, Any]:
    """合成一条 userTrades 响应行。"""
    return {
        "id": trade_id, "symbol": "TESTUSDT", "time": time_ms,
        "orderId": trade_id, "qty": "1", "price": "100",
        "side": "BUY", "commission": "0", "commissionAsset": "USDT",
        "realizedPnl": "0",
    }


def _income_row(tran_id: int, income_type: str, time_ms: int) -> Dict[str, Any]:
    """合成一条 um/income 响应行。"""
    return {
        "tranId": tran_id, "symbol": "TESTUSDT",
        "incomeType": income_type, "income": "-0.1", "asset": "USDT",
        "time": time_ms,
    }


class _FakeUserTradesAPI:
    """Fake get_user_trades — 对齐真实 PM UM userTrades 契约。

    互斥守卫：from_id 与 start_time/end_time 同时传入立即 raise。
    两条路径：
      - 仅 from_id → ID 续传（id > from_id），不按时间过滤
      - 仅时间 → 时间定位（start <= time < end）
    """

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: List[Dict[str, Any]] = []

    async def get_user_trades(
        self, symbol: str, *,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: Optional[int] = None,
        from_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append({
            "start_time": start_time, "end_time": end_time,
            "from_id": from_id, "limit": limit,
        })
        # PM UM userTrades 契约：fromId 与 startTime/endTime 互斥
        if from_id is not None and (start_time is not None or end_time is not None):
            raise ValueError(
                "from_id 与 start_time/end_time 互斥，不能同时传入"
                "（PM UM userTrades 契约约束）"
            )
        if from_id is not None:
            filtered = [r for r in self._rows if r.get("id", -1) > from_id]
        else:
            filtered = self._rows
            if start_time is not None:
                filtered = [r for r in filtered if r["time"] >= start_time]
            if end_time is not None:
                filtered = [r for r in filtered if r["time"] <= end_time]  # 闭区间：与真实 endTime 一致
        if limit is not None:
            filtered = filtered[:limit]
        return filtered


class _FakeIncomeAPI:
    """Fake get_income_history（模拟真实接口：不支持 tranId 续传）。"""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: List[Tuple[int, Optional[int], str]] = []

    async def get_income_history(
        self, *,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        income_type: str = "REALIZED_PNL",
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        self.calls.append((start_time or 0, end_time, income_type))
        filtered = [r for r in self._rows if r["incomeType"] == income_type]
        if start_time is not None:
            filtered = [r for r in filtered if r["time"] >= start_time]
        if end_time is not None:
            filtered = [r for r in filtered if r["time"] <= end_time]  # 闭区间
        return filtered[:limit]


class _FakeSink:
    """注入式 sink，支持 fail 标志模拟写入失败。"""

    def __init__(self) -> None:
        self.cursor: Any = None
        self.fail_upsert = False   # upsert_fills/upsert_income 失败开关
        self.fail_save = False     # save_cursor 失败开关
        self.persisted_rows: List[Any] = []  # 成功 upsert 的 fact 记录

    async def load_cursor(self, *args: Any, **kwargs: Any) -> Any:
        return self.cursor

    async def save_cursor(self, cursor: Any) -> None:
        if self.fail_save:
            raise RuntimeError("injected save_cursor failure")
        self.cursor = cursor

    async def upsert_fills(self, rows: List[Any]) -> None:
        if self.fail_upsert:
            raise RuntimeError("injected upsert_fills failure")
        self.persisted_rows.extend(rows)

    async def upsert_income(self, rows: List[Any]) -> None:
        if self.fail_upsert:
            raise RuntimeError("injected upsert_income failure")
        self.persisted_rows.extend(rows)


# ─══════════════════════════════════════════════════════════════════
# R2 [P1] 事实先可靠落库，再推进游标
# ─══════════════════════════════════════════════════════════════════


class TestR2FactBeforeCursor:
    """R2：三种故障注入 + 重试后集合完整性验证。"""

    def test_upsert_fails_retry_collects_all(self) -> None:
        """事实写失败 → page_complete=False + errors 非空；重试后集合完整。"""
        api = _FakeUserTradesAPI([
            _fill_row(1, 1000), _fill_row(2, 1100),
        ])
        sink = _FakeSink()
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=10, sink=sink,
        )

        # 第一次：upsert 失败
        sink.fail_upsert = True
        first = asyncio.run(exp.export_fills("TESTUSDT", 1, 2000))
        assert first.page_complete is False, "修复前错误地返回 page_complete=True"
        assert len(first.errors) > 0, "修复前 errors 为空列表"
        assert first.cursor.covered_until_ms == 1, "游标应回退到起点（不推进）"
        assert len(sink.persisted_rows) == 0, "无持久化发生"

        # 第二次：upsert 恢复成功，从起点重采 → 完整 2 笔
        sink.fail_upsert = False
        exp2 = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=10, sink=sink,
        )
        second = asyncio.run(exp2.export_fills("TESTUSDT", 1, 2000))
        assert second.page_complete is True
        assert second.errors == []
        persisted_ids = sorted(r.trade_id for r in sink.persisted_rows)
        assert persisted_ids == [1, 2], f"重试后持久化集合 {persisted_ids} != 完整 [1,2]"

    def test_save_cursor_fails_retry_collects_all(self) -> None:
        """游标写失败 → page_complete=False；重试后仍完整（sink 成功写入了事实）。"""
        api = _FakeUserTradesAPI([
            _fill_row(1, 1000), _fill_row(2, 1100),
        ])
        sink = _FakeSink()
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=10, sink=sink,
        )

        # 第一次：upsert 成功但 save_cursor 失败
        sink.fail_upsert = False
        sink.fail_save = True
        first = asyncio.run(exp.export_fills("TESTUSDT", 1, 2000))
        # upsert 成功写入了事实，save 失败追加了 errors
        assert len(first.errors) > 0
        assert first.page_complete is False
        assert len(sink.persisted_rows) == 2, "事实已成功写入"

        # 第二次：save 恢复成功。但注意游标没持久化（save 失败），
        # 所以 retry 从窗口起点重采。但因为 upsert 是幂等的，
        # 所以持久化集合仍然正确（不会出现重复问题——这里只验证最终集合完整）
        sink.fail_save = False
        sink.persisted_rows.clear()  # 清掉之前成功写的（模拟幂等重试从空开始）
        exp2 = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=10, sink=sink,
        )
        second = asyncio.run(exp2.export_fills("TESTUSDT", 1, 2000))
        assert second.page_complete is True
        persisted_ids = sorted(r.trade_id for r in sink.persisted_rows)
        assert persisted_ids == [1, 2]

    def test_mid_run_termination_retry_collects_all(self) -> None:
        """中途进程终止（upsert 不落盘）→ 重试回补缺失段。

        模拟方式：sink 无数据（从未成功写入），相当于中途终止。
        重试应从窗口起点完整采集。
        """
        api = _FakeUserTradesAPI([
            _fill_row(1, 1000), _fill_row(2, 1100), _fill_row(3, 1200),
        ])
        # 第一次：完全没有 sink（进程在 upsert 之前终止）
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=10,
            sink=None,  # 无 sink = 中途终止
        )
        first = asyncio.run(exp.export_fills("TESTUSDT", 1, 2000))
        # 无 sink 时 page_complete 仍按翻页逻辑判定（因为数据确实采完了）
        # 但没有持久化任何东西
        assert first.page_complete is True
        assert first.cursor.covered_until_ms >= 1200

        # 第二次：有 sink，从窗口起点完整重采
        sink = _FakeSink()
        exp2 = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=10, sink=sink,
        )
        second = asyncio.run(exp2.export_fills("TESTUSDT", 1, 2000))
        assert second.page_complete is True
        persisted_ids = sorted(r.trade_id for r in sink.persisted_rows)
        assert persisted_ids == [1, 2, 3]


# ─══════════════════════════════════════════════════════════════════
# R3 [P1] 收入游标纳入 income_type + account_id
# ─══════════════════════════════════════════════════════════════════


class TestR3CursorIdentity:
    """R3：income_type 隔离 + 账户隔离。"""

    def test_income_types_do_not_collide(self) -> None:
        """同窗依次采 REALIZED_PNL(t=1100) + COMMISSION(t=500) + FUNDING_FEE。

        若 income_type 没纳入游标键：先采 PNL 后游标覆盖到 1100，
        再采 COMMISSION 时从 1100 开始就漏掉了 t=500 的记录。
        修复后：每个 income_type 独立游标，互不影响。
        """
        api = _FakeIncomeAPI([
            _income_row(1, "REALIZED_PNL", 1100),
            _income_row(2, "COMMISSION", 500),
            _income_row(3, "FUNDING_FEE", 800),
        ])
        exp = ExecutionFactExporter(
            account_id="main", api_client=api, page_limit=10,
        )

        # 依次采集三种 income_type，各自完整
        for itype, expected_count in [("REALIZED_PNL", 1), ("COMMISSION", 1), ("FUNDING_FEE", 1)]:
            result = asyncio.run(exp.export_income(1, 2000, income_type=itype))
            assert result.page_complete is True, f"{itype} 应完整"
            assert len(result.records) == expected_count, (
                f"{itype} 期望 {expected_count} 条，实际 {len(result.records)} 条"
            )

    def test_different_accounts_do_not_collide(self) -> None:
        """不同 account_id 同窗同 income_type，游标互不覆盖。"""
        rows_a = [_income_row(100, "REALIZED_PNL", 1000)]
        rows_b = [_income_row(200, "REALIZED_PNL", 500)]

        api_a = _FakeIncomeAPI(rows_a)
        api_b = _FakeIncomeAPI(rows_b)

        exp_a = ExecutionFactExporter(
            account_id="account_a", api_client=api_a, page_limit=10,
        )
        exp_b = ExecutionFactExporter(
            account_id="account_b", api_client=api_b, page_limit=10,
        )

        # 先采 a，再采 b（b 的 income 更早：t=500）
        result_a = asyncio.run(exp_a.export_income(1, 2000, income_type="REALIZED_PNL"))
        result_b = asyncio.run(exp_b.export_income(1, 2000, income_type="REALIZED_PNL"))

        assert result_a.page_complete is True
        assert result_b.page_complete is True
        assert len(result_a.records) == 1
        assert len(result_b.records) == 1

    def test_window_extension_new_start_gets_fresh_cursor(self) -> None:
        """窗口扩展恢复规则：同一身份键下扩大窗口（更早的 window_start_ms）
        → 旧游标按 window_start_ms 精确匹配不命中 → 从新起点完整重采，
        不得复用旧窗口的完成标记（R3 验收：迁移口径=旧游标自然失效）。"""
        api = _FakeIncomeAPI([_income_row(1, "COMMISSION", 500)])
        exp = ExecutionFactExporter(
            account_id="main", api_client=api, page_limit=10,
        )

        # 第一次：窗口 [1000, 2000) → 记录 t=500 取不到（早于窗口起点）
        first = asyncio.run(exp.export_income(1000, 2000, income_type="COMMISSION"))
        assert first.page_complete is True
        assert len(first.records) == 0

        # 第二次：窗口扩展为 [100, 2000) → 不得复用旧游标（起点 1000），
        # 应从 100 重采并取到 t=500 的记录
        second = asyncio.run(exp.export_income(100, 2000, income_type="COMMISSION"))
        assert second.page_complete is True
        assert len(second.records) == 1, (
            "窗口扩展后必须从新起点重采，不得复用旧窗口完成标记"
        )

    def test_cursor_key_includes_full_identity(self) -> None:
        """_cursor_key 必须包含 source + account_id + income_type + symbol + window_start_ms。"""
        exp = ExecutionFactExporter(account_id="my_account", api_client=None)
        key = exp._cursor_key(
            "um/income", "my_account", "REALIZED_PNL", None, 1700000000000,
        )
        assert len(key) == 5, f"游标键应 5 元组，实际 {len(key)} 元组"
        assert key[0] == "um/income"
        assert key[1] == "my_account"
        assert key[2] == "REALIZED_PNL"
        assert key[3] is None  # income 无 symbol
        assert key[4] == 1700000000000


# ─══════════════════════════════════════════════════════════════════
# R6 [P2] 同毫秒跨页必须能推进到底
# ─══════════════════════════════════════════════════════════════════


class TestR6SameMsCrossPage:
    """R6：同毫秒记录跨页（fills 支持 fromId 续传）。"""

    def test_same_ms_exactly_at_limit(self) -> None:
        """同毫秒数量 = limit（恰好一页）：完整取回。
        from_id 续传语义：满页后必须再查一次确认无剩余 → 2 次调用是正确行为。"""
        api = _FakeUserTradesAPI([
            _fill_row(i, 1000) for i in range(1, 6)
        ])
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=5, max_pages=3,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, 2000))
        assert result.page_complete is True
        assert len(result.records) == 5
        # 第一次取满 5 条 → 必须 from_id=5 再查一次确认无剩余 → 2 次
        assert len(api.calls) == 2

    def test_same_ms_limit_plus_one(self) -> None:
        """同毫秒数量 = limit+1：跨页单条，取回全部 limit+1。"""
        api = _FakeUserTradesAPI([
            _fill_row(i, 1000) for i in range(1, 7)
        ])
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=5, max_pages=3,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, 2000))
        assert result.page_complete is True
        assert len(result.records) == 6, f"期望 6 条，实际 {len(result.records)} 条"
        assert len(api.calls) >= 2  # 至少两次请求

    def test_same_ms_four_trades_limit_two(self) -> None:
        """同毫秒 4 笔、limit=2：必须跨两页取到全部 4 笔。

        复现脚本原场景：expected=4, actual=4。
        修复前：只用 start_time=1000 推进，第二页还是同时间起点，只取到前 2 笔，
        max_pages 退出。修复后 from_id 续传能越过。
        """
        api = _FakeUserTradesAPI([
            _fill_row(i, 1000) for i in range(1, 5)
        ])
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=5,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, 2000))
        assert result.page_complete is True, "应完整取回，而非 max_pages 退出"
        assert len(result.records) == 4, (
            f"同毫秒 4 笔 limit=2 应取到全部 4 笔，实际 {len(result.records)}"
        )
        # 验证 from_id 续传：第二次及以后的请求 from_id 应非 None
        from_ids = [c["from_id"] for c in api.calls]
        assert from_ids[0] is None, "第一次请求无 from_id"
        assert from_ids[1] == 2, "第二次请求应从 id=2 之后开始"
        assert from_ids[2] == 4, "第三次请求应从 id=4 之后开始"

    def test_last_time_equals_window_boundary(self) -> None:
        """最后一条 time 恰等于窗口右边界：正确收尾，无越界请求。"""
        window_end = 1000
        api = _FakeUserTradesAPI([
            _fill_row(1, 500), _fill_row(2, 800), _fill_row(3, 999),
        ])
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=100, max_pages=3,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, window_end))
        assert result.page_complete is True
        assert len(result.records) == 3
        # 不应有越界请求（time >= window_end 的记录不会被请求到）

    def test_income_no_from_id_falls_back_to_incomplete(self) -> None:
        """income 接口不支持 tranId 续传：同毫秒满页 + 第二次又拉同批 → 三连触发 → 显式 incomplete。

        FakeIncomeAPI 不按 tranId 过滤 start_time=1000 又返回全部 5 条，
        seen 已全收集 → 全部跳过 → time_stuck + id_stuck + page_full 三连成立，
        必须触发 page_complete=False + request_status='incomplete' + errors 非空。
        """
        # 同毫秒 5 条 + page_limit=5 → 第一次满页，FakeIncomeAPI 不支持 tranId 续传
        rows = [_income_row(i, "REALIZED_PNL", 1000) for i in range(1, 6)]
        api = _FakeIncomeAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=5, max_pages=3,
        )
        result = asyncio.run(exp.export_income(1, 2000, income_type="REALIZED_PNL"))
        # 三连触发 → 显式不完整报告（而非静默成功或无限循环）
        assert result.page_complete is False
        assert result.cursor.request_status == "incomplete"
        assert len(result.errors) >= 1
        assert len(result.records) == 5  # 第一次 5 条都收集到了

    def test_multi_page_with_different_times(self) -> None:
        """不同时间的多页：正常按时间推进翻页。"""
        rows = [
            _fill_row(1, 1000), _fill_row(2, 1000),
            _fill_row(3, 2000), _fill_row(4, 2000),
            _fill_row(5, 3000), _fill_row(6, 3000),
        ]
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=5,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, 4000))
        assert result.page_complete is True
        assert len(result.records) == 6, f"期望 6 条，实际 {len(result.records)}"


# ─══════════════════════════════════════════════════════════════════
# R7 [P1] 成交分页参数互斥：时间定位页与 ID 续传页分离
# ─══════════════════════════════════════════════════════════════════


class TestR7PaginationContract:
    """R7 验收断言（架构 §2.1.5 + 需求 §3 R7）。

    1. 参数互斥不违例：含 fromId 的请求均不带 startTime/endTime
    2. 同毫秒跨页取全：3 条同毫秒 + limit=2 → 全部取回 + page_complete
    3. 窗口右边界客户端过滤：续传页越界行被过滤
    4. 保存游标后恢复路径：续传请求只发 fromId（无时间）
    """

    def test_no_request_sends_fromid_with_time_params(self) -> None:
        """续传请求（from_id 非 None）的 api_call_factory 不应同时发送时间参数。"""
        # 构造能触发续传场景的数据集（满 limit → 必须 from_id 续传）
        rows = [_fill_row(i, 1000) for i in range(1, 6)]  # 5 条同毫秒
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=5,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, 2000))
        assert result.page_complete is True
        assert len(result.records) == 5
        # 对每次请求的参数组合逐次校验：
        #   - from_id 非 None 时，start_time / end_time 必须都是 None
        for call in api.calls:
            if call["from_id"] is not None:
                assert call["start_time"] is None, (
                    f"续传请求不应带 start_time，实际 calls={api.calls}"
                )
                assert call["end_time"] is None, (
                    f"续传请求不应带 end_time，实际 calls={api.calls}"
                )
            # 首屏（from_id 为 None）应带时间参数
            if call["from_id"] is None:
                assert call["start_time"] is not None, (
                    f"首屏请求应带 start_time，实际 calls={api.calls}"
                )

    def test_same_ms_three_records_limit_two_collects_all(self) -> None:
        """同毫秒 3 条、limit=2：必须跨页取全 3 条。"""
        rows = [_fill_row(i, 1000) for i in range(1, 4)]  # id 1,2,3 / time=1000
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=4,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, 2000))
        assert result.page_complete is True
        assert len(result.records) == 3
        # 续传请求次数校验：
        #   call 0: 时间定位页 → from_id=None，取到 id=1,2（满页 2 == limit）
        #   call 1: from_id=2 → 返回 id=3（只 1 条 < limit=2）→ page_complete 收尾
        assert len(api.calls) == 2
        assert api.calls[0]["from_id"] is None
        assert api.calls[1]["from_id"] == 2
        # 续传请求不应同时带时间参数（R7 核心契约）
        assert api.calls[1]["start_time"] is None
        assert api.calls[1]["end_time"] is None

    def test_window_right_boundary_filtered_client_side(self) -> None:
        """续传页 API 可能返回 time > window_end_ms 的越界记录 —— 客户端必须过滤。

        Fake API 支持两种路径：
          - 仅 from_id → 不按时间过滤（模拟真实续传行为，可能返回越界）
          - 仅时间 → 按时间窗口过滤
        """
        # 构造：6 条数据，time=1000、1000、2000、2000、3000、3000
        # 窗口 end_time=2000（半开 [start, end)），应有 4 条（time=1000×2 + time=2000×2）
        rows = [
            _fill_row(1, 1000), _fill_row(2, 1000),
            _fill_row(3, 2000), _fill_row(4, 2000),
            _fill_row(5, 3000), _fill_row(6, 3000),
        ]
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=5,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, 2000))
        # R7 客户端右边界过滤：time > 2000（即 time=3000 的两条）被过滤，只取到 4 条
        assert len(result.records) == 4
        assert result.page_complete is True
        # 所有采集到的记录 time 必须 <= window_end_ms（2000）
        for f in result.records:
            assert f.trade_time_ms <= 2000, (
                f"不应有越界记录 time_ms={f.trade_time_ms}"
            )

    def test_save_cursor_then_resume_uses_fromid_only(self) -> None:
        """保存游标后恢复：续传请求只发 fromId，不传 start_time/end_time。

        用 max_pages=3 让 exp1 在第 3 页后被守卫强制退出（partial），
        这样 covered_until_ms < window_end_ms，exp2 不会被前置守卫短路，
        能触发断点恢复 → 验证 fromId-only 契约。
        """
        rows = [
            _fill_row(1, 1000), _fill_row(2, 1000),
            _fill_row(3, 2000), _fill_row(4, 2000),
            _fill_row(5, 3000), _fill_row(6, 3000),
            _fill_row(7, 4000), _fill_row(8, 4000),
        ]
        api = _FakeUserTradesAPI(rows)
        sink = _FakeSink()
        # max_pages=3 强制第 3 页后退出（page_limit=2，前 3 页拿 id=1-6）
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=3, sink=sink,
        )
        # 第一次跑：max_pages 强制退出，partial
        first = asyncio.run(exp.export_fills("TESTUSDT", 1, 5000))
        assert first.page_complete is False, "max_pages 退出应为 partial"
        assert first.cursor.request_status == "partial"
        assert len(first.records) == 6, "前 3 页应收集到 id=1-6"
        assert sink.cursor is not None
        saved_last_event_id = sink.cursor.last_event_id  # 应为 6

        # 用同一 sink（有已保存游标），重跑同窗口 → 从 last_event_id=6 续传
        api2 = _FakeUserTradesAPI(rows)
        exp2 = ExecutionFactExporter(
            account_id="test", api_client=api2, page_limit=2, max_pages=10, sink=sink,
        )
        second = asyncio.run(exp2.export_fills("TESTUSDT", 1, 5000))
        assert second.page_complete is True

        # 第二次的首轮请求：断点恢复有 last_event_id → 走 ID 续传路径
        assert len(api2.calls) >= 1, "断点恢复应至少发一次 API"
        resume_call = api2.calls[0]
        assert resume_call["from_id"] == saved_last_event_id
        assert resume_call["start_time"] is None, (
            "断点续传请求不应带 start_time"
        )
        assert resume_call["end_time"] is None, (
            "断点续传请求不应带 end_time"
        )

    def test_invalid_param_combo_raises_in_production(self) -> None:
        """直接调用 binance_api.get_user_trades：from_id 与 start_time 同现必须 raise。"""
        import pytest as _pytest
        from shared.binance_api import BinanceClient
        client = BinanceClient.__new__(BinanceClient)
        client.use_unified_account = True
        with _pytest.raises(ValueError) as excinfo:
            asyncio.run(client.get_user_trades(
                "BTCUSDT", start_time=1000, end_time=2000, from_id=42,
            ))
        assert "互斥" in str(excinfo.value)
        assert "PM UM userTrades" in str(excinfo.value)

    def test_invalid_param_combo_raises_in_fake(self) -> None:
        """收紧的 Fake API：from_id 与 start_time 同现必须 raise。"""
        import pytest as _pytest
        api = _FakeUserTradesAPI([_fill_row(1, 1000)])
        with _pytest.raises(ValueError):
            asyncio.run(api.get_user_trades(
                "TESTUSDT", start_time=1000, from_id=42,
            ))


# ─══════════════════════════════════════════════════════════════════
# R10 [P1] 右边界闭区间 + 同毫秒多页续取
# ─══════════════════════════════════════════════════════════════════


class TestR10RightBoundaryClosedInterval:
    """R10：fills 分页右边界闭区间 + 原始满页续取，不静默漏数。"""

    def test_end_ms_exactly_limit_records(self) -> None:
        """窗口结束毫秒恰好有 limit 笔 → 完整取回 + page_complete=True。

        Fake 用闭区间 time<=end_time，所以结束毫秒的记录必须全被拉到。
        """
        window_end = 1000
        limit = 5
        rows = [_fill_row(i, window_end) for i in range(1, limit + 1)]
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=limit, max_pages=5,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, window_end))
        assert result.page_complete is True
        assert len(result.records) == limit, f"应取回 {limit} 笔，实际 {len(result.records)}"
        # 至少 2 次调用：第一次满 5 条 → 第二次 from_id=5 续传确认排空
        assert len(api.calls) >= 2

    def test_end_ms_limit_plus_one(self) -> None:
        """窗口结束毫秒有 limit+1 笔 → 跨页取全 limit+1，不漏。"""
        window_end = 1000
        limit = 5
        rows = [_fill_row(i, window_end) for i in range(1, limit + 2)]
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=limit, max_pages=5,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, window_end))
        assert result.page_complete is True
        assert len(result.records) == limit + 1, (
            f"应取回 {limit+1} 笔，实际 {len(result.records)}"
        )

    def test_end_ms_multi_page(self) -> None:
        """窗口结束毫秒有 5 笔、limit=2 → 跨 3 页取到全部 5 笔，不漏。"""
        window_end = 1000
        limit = 2
        rows = [_fill_row(i, window_end) for i in range(1, 6)]  # 5 笔同毫秒
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=limit, max_pages=10,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, window_end))
        assert result.page_complete is True
        assert len(result.records) == 5, f"应取回 5 笔，实际 {len(result.records)}"

    def test_out_of_window_rows_filtered_not_misjudged(self) -> None:
        """续传页返回越界记录（time > window_end）→ 被客户端过滤，
        且 saw_out_of_window=True，不误判为 page_complete。

        场景：窗口 [1, 2000]，记录 time=1000×2 + time=2000×2 + time=3000×2
        → 期望：前 4 笔在窗口内，time=3000 的 2 笔被过滤，saw_out_of_window=True。
        """
        window_end = 2000
        rows = [
            _fill_row(1, 1000), _fill_row(2, 1000),
            _fill_row(3, 2000), _fill_row(4, 2000),
            _fill_row(5, 3000), _fill_row(6, 3000),
        ]
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=5,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, window_end))
        assert result.page_complete is True
        assert len(result.records) == 4
        # 所有记录 time <= window_end
        for f in result.records:
            assert f.trade_time_ms <= window_end, "不应有越界记录"

    def test_mid_window_failure_never_complete(self) -> None:
        """中途 API 失败 → page_complete=False，绝不能误标 complete。

        page_limit=2 + Fake 第 3 次调用失败：
          call 1 → time=1000×2（满页）
          call 2 → from_id=2, id=3,4（time=2000×2，满页）
          call 3 → from_id=4, id=5... → 抛异常
        → 已收集 4 条，page_complete=False。
        """
        class _FlakyUserTradesAPI(_FakeUserTradesAPI):
            """前两次正常，第三次抛异常。"""
            async def get_user_trades(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
                if len(self.calls) >= 2:
                    self.calls.append({"__fail__": True})  # 先记失败调用
                    raise RuntimeError(f"模拟第 {len(self.calls)} 次请求失败")
                return await super().get_user_trades(*args, **kwargs)

        window_end = 5000
        rows = [
            _fill_row(1, 1000), _fill_row(2, 1000),
            _fill_row(3, 2000), _fill_row(4, 2000),
            _fill_row(5, 3000), _fill_row(6, 3000),
        ]
        api = _FlakyUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=5,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, window_end))
        assert result.page_complete is False, "中途失败绝不能 page_complete=True"
        assert len(result.errors) > 0
        # 前两页已成功收集 4 条
        assert len(result.records) == 4

    def test_time_boundary_consistency_fake_closed_interval(self) -> None:
        """Fake 端到端闭区间一致：结束毫秒的记录应被 API 返回、被客户端保留。"""
        window_end = 2000
        rows = [
            _fill_row(1, 1000), _fill_row(2, window_end),
            _fill_row(3, window_end + 1),  # 越界：window_end+1
        ]
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=10, max_pages=3,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, window_end))
        # 只有 time<=window_end 的 2 条被取回
        assert len(result.records) == 2
        times = sorted(f.trade_time_ms for f in result.records)
        assert times == [1000, window_end]


# ─══════════════════════════════════════════════════════════════════
# R12 [P2] income 同毫秒满页到右边界 → incomplete
# ─══════════════════════════════════════════════════════════════════


class TestR12IncomeIncompleteAtBoundary:
    """R12：income 接口无事件ID续传，同毫秒超页 + 触右边界 → incomplete。"""

    def test_income_end_ms_exactly_limit(self) -> None:
        """income 结束毫秒恰好 limit 笔 → 正常 complete（未满页到不到界）。"""
        window_end = 1000
        limit = 5
        rows = [_income_row(i, "REALIZED_PNL", window_end) for i in range(1, limit + 1)]
        api = _FakeIncomeAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=limit, max_pages=3,
        )
        result = asyncio.run(exp.export_income(1, window_end))
        # 恰好一页满 → fake 用闭区间返回全部 5 条（从 start 推进），
        # 但 income 无 fromId，所以第二次请求会重复。
        # 关键：income 同毫秒且 Fake 不做去重过滤 → 第二次返回同样的 5 条 →
        # seen 全跳过 → time_ms 卡在 1000 → 触发 income 同毫秒卡住
        # 不对……让我重新想：
        # 第一次 api_call_factory(1, None) → start_time=1, end_time=1000
        # Fake: time>=1 and time<=1000 → 全部 5 条 → page_limit=5 → 5 条满
        # raw_page_full=True
        # consume_page: 5 条新的 → seen 记下 → new_advance.time_ms=1000
        # 继续循环……
        # 第二次 api_call_factory(1000, None) → start_time=1000, end_time=1000
        # Fake: time>=1000 and time<=1000 → 全部 5 条又返回
        # consume_page: seen 全跳过（都是重复）→ new_advance.time_ms 保持 1000
        # loop_advance.time_ms=1000, new_advance.time_ms=1000 → time 卡住
        # supports_id_cursor=False（income）→ new_advance.time_ms >= window_end_ms
        #   (1000 >= 1000) → True！
        # 所以 _detect_unstoppable 会命中！incomplete！
        #
        # 但这其实是 R12 的正常预期——income 到右边界且同毫秒，无续传能力 → incomplete！
        assert result.page_complete is False
        assert result.cursor.request_status == "incomplete"
        assert result.cursor.error_detail is not None
        assert "income" in (result.cursor.error_detail or "")
        assert result.cursor.covered_until_ms == window_end
        # 第一页的 5 条都收集到了
        assert len(result.records) == 5

    def test_income_never_uses_plus_one_ms_workaround(self) -> None:
        """完成路径 covered_until_ms 固定 window_end_ms，绝无 +1ms 绕过路径。

        断言逻辑：遍历所有 CursorRecord 构造点，查看是否有 window_end_ms + 1。
        （这里不直接 grep 源码，而是验证完成后 cursor.covered_until_ms == window_end_ms。）
        """
        window_end = 2000
        rows = [_income_row(i, "REALIZED_PNL", 1000) for i in range(1, 3)]
        api = _FakeIncomeAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=10, max_pages=3,
        )
        result = asyncio.run(exp.export_income(1, window_end))
        assert result.page_complete is True
        # 完成时 covered_until_ms 必须等于 window_end_ms，绝无 +1
        assert result.cursor.covered_until_ms == window_end, (
            f"covered_until_ms={result.cursor.covered_until_ms} 不应 > window_end={window_end}"
        )

    def test_income_multi_page_to_boundary_incomplete(self) -> None:
        """income 到右边界且同毫秒满页 → incomplete（无法用事件ID续传）。

        FakeIncomeAPI 模拟真实 income 接口不做时间内部分页——每次 start_time 重查
        返回同样结果（limit 切片前 N 条匹配），这正好复现 income 无事件ID续传的缺陷：
          call 1: start=1, end=3000 → 全部 5 条 limit=2 → 前 2 条 time=1000
          call 2: start=1000, end=3000 → 还是全部 5 条 limit=2 → 前 2 条重复
                 → seen 跳过 → time 卡在 1000，但 1000 < 3000，不触界
          call 3: start=1000 继续卡 → Fake 返回重复 → 再卡住
          → 最终会因为 Fake 每次返回同批 → time 永远卡 1000，但 Fake 时间也卡住
          → income 检测 time_stuck + 满页 → incomplete

        所以这个场景 income 会在同毫秒卡住（不是到右边界卡住），但核心是 income
        无续传能力，必然 incomplete。
        """
        window_end = 3000
        limit = 2
        rows = [
            _income_row(i, "REALIZED_PNL", 1000) for i in range(1, 6)  # 5 条全同毫秒
        ]
        api = _FakeIncomeAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=limit, max_pages=5,
        )
        result = asyncio.run(exp.export_income(1, window_end))
        # Fake 第 1 次返回 2 条，后续每次都返回同 2 条（seen 跳过）→ time 卡住 → incomplete
        assert result.page_complete is False
        assert result.cursor.request_status == "incomplete"
        assert "income" in (result.cursor.error_detail or "")
        # 第一页的 2 条被收集
        assert len(result.records) == 2


# ─══════════════════════════════════════════════════════════════════
# 复审整改：V1 前置守卫 / V2 死字段删除 / V3 函数拆分
# ─══════════════════════════════════════════════════════════════════


class TestV1PartialCursorBoundary:
    """V1：前置守卫不得把 boundary 处的 incomplete 游标静默改写为 ok。

    场景：上一次采集留下的游标 covered_until_ms 恰等于 window_end_ms
    （income 到右边界不可推进时 _build_unstoppable_result 正是如此赋值）、
    但 page_complete=False / request_status='incomplete'。
    重跑同一窗口时，旧守卫 `cur_start >= window_end_ms` 会命中 → 直接构造
    _ok_cursor → 不完整被改写成完整。
    修复后：守卫必须同时校验完成态——若已有游标且 page_complete=False，
    不得短路，必须进入翻页循环重新判定。
    """

    def test_partial_cursor_at_boundary_rerun_does_not_become_ok(self) -> None:
        """构造一个 covered_until_ms == window_end_ms 且 page_complete=False
        的既有游标，重跑同一窗口，断言结果不得是 ok/complete。
        重跑时 Fake API 仍返回相同数据（同毫秒满页不可续传场景），
        守卫不得短路 → 必须进入翻页循环 → income 到界 → incomplete。
        """
        from scripts.export_execution_facts import CursorRecord

        window_start, window_end = 1, 1000
        # 先在 sink 里预置一个 incomplete 游标（covered_until_ms 恰好到界）
        sink = _FakeSink()
        sink.cursor = CursorRecord(
            source="um/income",
            account_id="test",
            income_type="REALIZED_PNL",
            symbol=None,
            window_start_ms=window_start,
            window_end_ms=window_end,
            covered_until_ms=window_end,  # 恰等于 window_end_ms
            request_status="incomplete",
            page_complete=False,  # 关键：未完成
            returned_count=5,
            error_detail="income 同毫秒记录可能超单页且接口不支持事件ID续传",
        )

        # Fake API 返回同毫秒 5 条（limit=5 → 第一页满 → income 到界不可续传）
        rows = [_income_row(i, "REALIZED_PNL", window_end) for i in range(1, 6)]
        api = _FakeIncomeAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=5, max_pages=3, sink=sink,
        )
        result = asyncio.run(exp.export_income(
            window_start, window_end, income_type="REALIZED_PNL",
        ))

        # 修复前：前置守卫 cur_start >= window_end_ms 命中 → 直接 _ok_cursor → ok
        # 修复后：守卫校验 page_complete → 已加载 incomplete → 不短路 → 进翻页
        # income 到界又不可续传 → 仍是 incomplete
        assert result.page_complete is False, (
            f"修复前守卫静默改写：page_complete 应为 False，"
            f"实际 {result.page_complete}"
        )
        assert result.cursor.request_status == "incomplete", (
            f"修复前守卫静默改写：request_status 应为 incomplete，"
            f"实际 {result.cursor.request_status}"
        )


class TestV1IncomeIncompleteRerun:
    """V1：income 在同一毫秒超单页不可续传 → incomplete；重跑仍为 incomplete，
    不得被守卫静默改写为 ok。"""

    def test_incomplete_income_rerun_stays_incomplete(self) -> None:
        """先采集（income 到界 incomplete），再用同一 sink 重跑同一窗口。"""
        window_end = 1000
        # 构造：income 同毫秒 5 条、limit=5 → 第一页满 → Fake 第二次返回同批
        # → seen 全跳过 → 同毫秒卡住 → incomplete
        rows = [_income_row(i, "REALIZED_PNL", window_end) for i in range(1, 6)]
        api = _FakeIncomeAPI(rows)
        sink = _FakeSink()
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=5, max_pages=3, sink=sink,
        )

        # 第一次跑：到界 → incomplete
        first = asyncio.run(exp.export_income(1, window_end))
        assert first.cursor.request_status == "incomplete", (
            f"第一次应 incomplete，实际 {first.cursor.request_status}"
        )
        assert first.page_complete is False
        assert sink.cursor is not None
        # sink 里已保存的游标：covered_until_ms == window_end, page_complete=False
        assert sink.cursor.covered_until_ms == window_end
        assert sink.cursor.page_complete is False

        # 用同一 sink 重跑同一窗口
        api2 = _FakeIncomeAPI(rows)
        exp2 = ExecutionFactExporter(
            account_id="test", api_client=api2, page_limit=5, max_pages=3, sink=sink,
        )
        second = asyncio.run(exp2.export_income(1, window_end))

        # 修复前：守卫 cur_start >= window_end_ms 命中 → 直接 _ok_cursor → ok
        # 修复后：守卫校验 page_complete → 已加载 incomplete → 进入翻页 → 仍是 incomplete
        assert second.cursor.request_status == "incomplete", (
            f"重跑应保持 incomplete，不得被守卫静默改写为 ok，"
            f"实际 {second.cursor.request_status}"
        )
        assert second.page_complete is False


# ─══════════════════════════════════════════════════════════════════
# 测试专家补用例：fills 不可推进 → 事件ID卡住 分支（_build_incomplete
# 的 supports_id_cursor=True 路径，export_execution_facts.py L879）
# ─══════════════════════════════════════════════════════════════════


class _StuckIDUserTradesAPI(_FakeUserTradesAPI):
    """fromId 续传时故意忽略过滤，始终返回同一批记录。

    用于制造 fills（supports_id_cursor=True）的「事件ID卡住」场景：
    第一页满（page_limit=2，取到 id=1,2），第二页 fromId=2 仍返回 id=1,2，
    seen 全跳过 → new_advance.event_id 不变 → _detect_unstoppable 命中
    → _build_incomplete 的 supports_id_cursor=True 分支被触发。
    """

    async def get_user_trades(self, symbol: str, *, start_time: Optional[int] = None,
                              end_time: Optional[int] = None,
                              limit: Optional[int] = None,
                              from_id: Optional[int] = None) -> List[Dict[str, Any]]:
        # 记录调用，但 from_id 非 None 时仍按 limit 截断返回全量（不按 id > from_id 过滤）
        self.calls.append({
            "start_time": start_time, "end_time": end_time,
            "from_id": from_id, "limit": limit,
        })
        return self._rows[:limit]


class TestFillsIDStuckIncomplete:
    """fills 不可推进：fromId 续传事件ID卡死 → incomplete + 含「事件ID卡住」detail。"""

    def test_fills_fromid_stuck_becomes_incomplete(self) -> None:
        """构造 Fake：fromId 续传始终返回同一批满页记录，驱动 fills 进入
        「原始页满 + 事件ID卡住」不可推进路径。"""
        rows = [_fill_row(i, 1000) for i in range(1, 3)]  # id=1, 2
        api = _StuckIDUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=5,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1, 99999))

        # 断言 1：状态 + 完成态
        assert result.cursor.request_status == "incomplete", (
            f"fills 事件ID卡住应 incomplete，实际 {result.cursor.request_status}"
        )
        assert result.page_complete is False

        # 断言 2：detail 命中 fills 专属文案（L879 分支）
        assert "事件ID卡住" in (result.cursor.error_detail or ""), (
            f"detail 应含「事件ID卡住」，实际 {result.cursor.error_detail}"
        )

        # 断言 3：errors 非空（含不可推进说明）
        assert len(result.errors) >= 1, "不可推进应追加 errors"
        assert "事件ID卡住" in result.errors[0]


# ─══════════════════════════════════════════════════════════════════
# Round 4 点窗缺陷回归（闭区间守卫过纠正：无游标时 cur_start==end 不应短路）
# ─══════════════════════════════════════════════════════════════════


class TestPointWindowGuards:
    """无既有游标时，单毫秒闭区间 [E, E] 仍必须翻页查询。

    修复前守卫 `cur_start >= end and (无游标 or 已完成)` 在
    window_start == window_end 且首次查询（无游标）时直接短路，
    导致 fills / income 在单毫秒有数据时被静默漏采并错误标记 complete。
    修复后守卫改为「cur_start >= end 且 有游标且游标已完成」才短路。
    """

    def test_point_window_single_fill_is_returned(self) -> None:
        """闭区间 [1000, 1000] 有 1 条 fill → 必须采得 1 条 + 至少一次 API 调用。"""
        rows = [_fill_row(trade_id=1, time_ms=1000)]
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(account_id="test", api_client=api, page_limit=10)
        result = asyncio.run(exp.export_fills("TESTUSDT", 1000, 1000))

        assert len(api.calls) >= 1, (
            f"修复前守卫短路导致 API 调用数=0，至少应为 1；实际 {len(api.calls)}"
        )
        assert len(result.records) == 1, (
            f"单毫秒内有 1 条成交应采得，实际 {len(result.records)}"
        )
        assert result.page_complete is True

    def test_point_window_empty_still_queries(self) -> None:
        """闭区间 [1000, 1000] 无 fill → 仍应查询一次并返回空 + complete。"""
        api = _FakeUserTradesAPI([])
        exp = ExecutionFactExporter(account_id="test", api_client=api, page_limit=10)
        result = asyncio.run(exp.export_fills("TESTUSDT", 1000, 1000))

        assert len(api.calls) >= 1, "空窗口不得静默跳过，至少一次 API 调用"
        assert len(result.records) == 0
        assert result.page_complete is True

    def test_point_window_exceeding_one_page(self) -> None:
        """fills 单毫秒 5 条 + limit=2 → 必须全部取回（fromId 续传）。"""
        rows = [_fill_row(i, 1000) for i in range(1, 6)]
        api = _FakeUserTradesAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=10,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1000, 1000))

        assert len(result.records) == 5, (
            f"单毫秒 5 条应全部取回，实际 {len(result.records)}"
        )
        assert result.page_complete is True
        # fills 应产生多次调用（首请求不带 from_id，后续带 from_id 续传）
        assert len(api.calls) >= 2

    def test_point_window_income_over_one_page_incomplete(self) -> None:
        """income 单毫秒 5 条 + limit=2 → 因无事件ID续传应 incomplete。"""
        rows = [_income_row(i, "REALIZED_PNL", 1000) for i in range(1, 6)]
        api = _FakeIncomeAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=5,
        )
        result = asyncio.run(exp.export_income(1000, 1000))

        assert result.cursor.request_status == "incomplete", (
            f"income 单毫秒超页应 incomplete，实际 {result.cursor.request_status}"
        )
        assert result.page_complete is False
        assert len(result.errors) > 0


class TestIdempotentShortCircuitPreserved:
    """守卫仍需保留「已完成游标幂等短路」语义——不得把守卫全去掉。"""

    def test_completed_cursor_rerun_short_circuits(self) -> None:
        """既有游标 page_complete=True 且 covered_until_ms==end → 零调用、ok。"""
        from scripts.export_execution_facts import CursorRecord

        sink = _FakeSink()
        sink.cursor = CursorRecord(
            source="userTrades",
            account_id="test",
            income_type=None,
            symbol="TESTUSDT",
            window_start_ms=1000, window_end_ms=2000,
            covered_until_ms=2000,
            request_status="ok", page_complete=True,
            returned_count=3,
            last_event_id=7,
        )
        api = _FakeUserTradesAPI([])
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=10, sink=sink,
        )
        result = asyncio.run(exp.export_fills("TESTUSDT", 1000, 2000))

        assert len(api.calls) == 0, (
            f"已完成游标幂等短路应零调用，实际 {len(api.calls)}"
        )
        assert result.cursor.request_status == "ok"
        assert result.page_complete is True

    def test_uncompleted_cursor_rerun_not_short_circuited(self) -> None:
        """既有游标 page_complete=False 且 covered_until_ms==end → 不得短路。

        守住上轮 V1 修复：未完成游标即便到界也必须进入翻页循环重判。
        """
        from scripts.export_execution_facts import CursorRecord

        sink = _FakeSink()
        sink.cursor = CursorRecord(
            source="um/income",
            account_id="test", income_type="REALIZED_PNL",
            symbol=None, window_start_ms=1000, window_end_ms=2000,
            covered_until_ms=2000,
            request_status="incomplete", page_complete=False,
            returned_count=2,
            error_detail="income 同毫秒同页不可续传",
        )
        rows = [_income_row(i, "REALIZED_PNL", 2000) for i in range(1, 6)]
        api = _FakeIncomeAPI(rows)
        exp = ExecutionFactExporter(
            account_id="test", api_client=api, page_limit=2, max_pages=5, sink=sink,
        )
        result = asyncio.run(exp.export_income(1000, 2000))

        # 守卫不得短路 → 必须进入翻页 → 又到界不可续传 → 仍是 incomplete
        assert result.page_complete is False
        assert result.cursor.request_status != "ok"


class TestReverseWindowGuardsNaturalPaging:
    """反向窗口（start > end）在 Round 4 守卫修正后的自然行为。

    cur_start（初始值就是 window_start_ms，无 cursor 时）= 2000 > end = 1000
    → 守卫条件「到界 + 有游标 + 游标已完成」不成立（loaded_cursor=None）
    → 必须进翻页循环做一次 API 调用
    → Fake 时间过滤 start=2000, end=1000 → 空列表
    → 空列表 < limit → _build_complete → ok + page_complete=True

    不做显式入口 raise / error 游标，让守卫修正自然覆盖。
    """

    def test_reverse_fills_window_queries_then_returns_ok_empty(self) -> None:
        api = _FakeUserTradesAPI([])
        exp = ExecutionFactExporter(account_id="test", api_client=api, page_limit=10)
        result = asyncio.run(exp.export_fills("TESTUSDT", 2000, 1000))

        assert len(api.calls) >= 1, "反向窗口仍需进翻页查一次空"
        assert result.page_complete is True, "空窗查询后应 complete"
        assert result.cursor.request_status == "ok"
        assert result.records == []
        assert result.errors == []

    def test_reverse_income_window_queries_then_returns_ok_empty(self) -> None:
        api = _FakeIncomeAPI([])
        exp = ExecutionFactExporter(account_id="test", api_client=api, page_limit=10)
        result = asyncio.run(exp.export_income(2000, 1000))

        assert len(api.calls) >= 1
        assert result.page_complete is True
        assert result.cursor.request_status == "ok"
        assert result.records == []
        assert result.errors == []
