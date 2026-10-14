"""
HRS income 对账脚本单元测试

覆盖用例：
1. 时间换算正确性（UTC ms ↔ 北京 naive 往返）
2. Income 分片分页 + 完整性断言：
   - chunk 内部返回条数 < limit：停止续拉
   - 返回条数 == limit：必须续拉下一页
   - 返回条数 == limit 但 last_time <= cursor：标记截断，告警
   - 正常多 chunk 遍历
3. CSV 解析：空值、Decimal 转换、时间格式、PNL_SUMMARY 标记
4. 逐笔匹配逻辑：
   - 时间窗过滤（超出不匹配）
   - 金额容差（max(0.01, |income|*1%)）
   - 平仓单优先
   - 同 symbol 多策略歧义 → 未决
   - 一笔 income 对应多候选 → 金额最近优先
5. 真值表五口径计算（A/B/C/D/E）
6. 边界：空输入、无同 symbol、全部超窗

说明：所有用例均 mock BinanceClient 和 subprocess，不连真实 API / SSH。
"""

import csv
import itertools
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from ai_tuner.reconciler.hrs_income_reconcile import (  # noqa: E402
    HRSReconcileConfig,
    IncomeFetchResult,
    MatchResult,
    ReconcileSummary,
    MonthlyTruthRow,
    _bj_naive_to_utc_ms,
    _utc_ms_to_bj_naive,
    _dedupe_income,
    _normalize_trade_record,
    _compute_position_level_pnl,
    _pick_best_candidate,
    match_income_to_trades,
    compute_monthly_truth_table,
    fetch_income_sharded,
    load_trade_records_csv,
)

_CST = timezone(timedelta(hours=8))

# 全局递增计数器：保证每条测试构造的 trade_record 都有唯一 id
_id_counter = itertools.count(1000)

# mock income 时间基准：2026-07-01 00:00:00 UTC（确保落在 default_cfg 第一个 chunk 时间窗内）
_MOCK_BASE_MS = 1782835200000


# ======================================================================================
# Fixtures
# ======================================================================================

@pytest.fixture
def default_cfg():
    """默认对账配置（容差窗口缩小便于断言）"""
    return HRSReconcileConfig(
        chunk_days=3,
        batch_size=5,  # 用小 batch_size 方便触发分页场景
        match_window_ms=30 * 60 * 1000,  # 30 分钟
        binance_api_key="TEST_KEY_FOR_TESTS_ONLY",
        binance_api_secret="TEST_SECRET_FOR_TESTS_ONLY",
    )


@pytest.fixture
def small_window_cfg():
    """缩小时间窗配置（60 秒），便于构造精确匹配/不匹配场景"""
    return HRSReconcileConfig(
        chunk_days=3,
        batch_size=5,
        match_window_ms=60_000,  # 60 秒
        min_amount_tolerance=Decimal("0.01"),
        amount_tolerance_ratio=Decimal("0.01"),
    )


def _make_trade_record(
    record_id=None,
    strategy="hrs",
    symbol="BTRUSDT",
    order_type="TAKE_PROFIT",
    side="SELL",
    pnl=Decimal("-9.8343"),
    exec_time=None,
    close_reason=None,
    is_summary=False,
):
    """构造标准化 trade_record dict。不指定 record_id 时自动分配唯一递增 id。"""
    if record_id is None:
        record_id = str(next(_id_counter))
    if exec_time is None:
        exec_time = datetime(2026, 9, 1, 8, 30, 0)  # 北京 naive
    return {
        "id": record_id,
        "strategy": strategy,
        "symbol": symbol,
        "order_id": None,
        "side": side,
        "order_type": order_type if not is_summary else "PNL_SUMMARY",
        "quantity": Decimal("0.1"),
        "price": Decimal("98343"),
        "commission": Decimal("0.01"),
        "status": "FILLED",
        "executed_at": exec_time,
        "executed_at_bj_str": exec_time.strftime("%Y-%m-%d %H:%M:%S"),
        "realized_pnl": pnl,
        "close_reason": close_reason,
        "algo_id": None,
        "is_summary": is_summary,
    }


def _make_income(symbol="BTRUSDT", time_ms=None, amount=Decimal("-9.8343")):
    """构造 income JSON 字段（Binance API 返回格式）"""
    if time_ms is None:
        time_ms = _bj_naive_to_utc_ms(datetime(2026, 9, 1, 8, 30, 15))
    return {
        "symbol": symbol,
        "incomeType": "REALIZED_PNL",
        "income": str(amount),
        "asset": "USDT",
        "info": "",
        "time": time_ms,
        "tranId": 1234567890,
    }


# ======================================================================================
# 测试 1：时间换算正确性
# ======================================================================================

class TestTimeConversion:
    """UTC 毫秒 ↔ 北京 naive 往返"""

    def test_roundtrip(self):
        """北京 naive → UTC ms → 北京 naive 往返一致"""
        bj = datetime(2026, 9, 1, 8, 30, 0)
        ms = _bj_naive_to_utc_ms(bj)
        back = _utc_ms_to_bj_naive(ms)
        assert back == bj
        assert back.tzinfo is None

    def test_epoch(self):
        """0 ms → 北京 1970-01-01 08:00:00"""
        bj = _utc_ms_to_bj_naive(0)
        assert bj == datetime(1970, 1, 1, 8, 0, 0)
        assert bj.tzinfo is None

    def test_bj_to_utc_correct_offset(self):
        """北京 2026-09-01 00:00 → UTC 2026-08-31 16:00"""
        bj = datetime(2026, 9, 1, 0, 0, 0)
        ms = _bj_naive_to_utc_ms(bj)
        utc = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        assert utc.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-31 16:00:00"


# ======================================================================================
# 测试 2：Income 去重
# ======================================================================================

class TestIncomeDedupe:
    def test_same_symbol_time_income_deduped(self):
        records = [
            {"symbol": "BTCUSDT", "time": 1700000000000, "income": "1.5"},
            {"symbol": "BTCUSDT", "time": 1700000000000, "income": "1.5"},
        ]
        result = _dedupe_income(records)
        assert len(result) == 1

    def test_different_symbol_kept(self):
        records = [
            {"symbol": "BTCUSDT", "time": 1700000000000, "income": "1.5"},
            {"symbol": "ETHUSDT", "time": 1700000000000, "income": "1.5"},
        ]
        result = _dedupe_income(records)
        assert len(result) == 2


# ======================================================================================
# 测试 3：CSV 解析
# ======================================================================================

class TestCSVParse:
    """_normalize_trade_record 字段解析"""

    def test_normal_row(self):
        row = {
            "id": "42", "strategy": "hrs", "symbol": "BTRUSDT",
            "order_id": "12345", "side": "SELL", "order_type": "TAKE_PROFIT",
            "quantity": "0.1", "price": "98343", "commission": "0.01",
            "status": "FILLED", "executed_at": "2026-09-01 08:30:00",
            "realized_pnl": "-9.8343", "close_reason": "", "algo_id": "",
        }
        rec = _normalize_trade_record(row)
        assert rec is not None
        assert rec["id"] == "42"
        assert rec["realized_pnl"] == Decimal("-9.8343")
        assert rec["executed_at"] == datetime(2026, 9, 1, 8, 30, 0)
        assert rec["is_summary"] is False
        assert rec["order_id"] == "12345"

    def test_empty_order_id(self):
        """空 order_id → None"""
        row = {
            "id": "42", "strategy": "hrs", "symbol": "BTRUSDT",
            "order_id": "", "side": "SELL", "order_type": "TAKE_PROFIT",
            "quantity": "0.1", "price": "98343", "commission": "0.01",
            "status": "FILLED", "executed_at": "2026-09-01 08:30:00",
            "realized_pnl": "-9.8343", "close_reason": "", "algo_id": "",
        }
        rec = _normalize_trade_record(row)
        assert rec["order_id"] is None

    def test_pnl_summary_row(self):
        """order_type='PNL_SUMMARY' → is_summary=True"""
        row = {
            "id": "43", "strategy": "hrs", "symbol": "BTRUSDT",
            "order_id": "", "side": "SELL", "order_type": "PNL_SUMMARY",
            "quantity": "0", "price": "0", "commission": "0",
            "status": "FILLED", "executed_at": "2026-09-01 10:00:00",
            "realized_pnl": "-20.5883", "close_reason": "", "algo_id": "",
        }
        rec = _normalize_trade_record(row)
        assert rec["is_summary"] is True

    def test_no_executed_at_returns_none(self):
        row = {
            "id": "99", "strategy": "hrs", "symbol": "BTRUSDT",
            "executed_at": "", "realized_pnl": "0",
        }
        # 补全最小必需字段
        row.update({"order_id": "", "side": "SELL", "order_type": "LIMIT",
                     "quantity": "0", "price": "0", "commission": "0",
                     "status": "", "close_reason": "", "algo_id": ""})
        rec = _normalize_trade_record(row)
        assert rec is None

    def test_realized_pnl_none(self):
        """realized_pnl 空串 → None（不是 Decimal('0')）"""
        row = {
            "id": "50", "strategy": "hrs", "symbol": "BTRUSDT",
            "order_id": "", "side": "SELL", "order_type": "LIMIT",
            "quantity": "0.1", "price": "98343", "commission": "0.01",
            "status": "FILLED", "executed_at": "2026-09-01 08:30:00",
            "realized_pnl": "", "close_reason": "", "algo_id": "",
        }
        rec = _normalize_trade_record(row)
        assert rec["realized_pnl"] is None

    def test_full_csv_load(self):
        """load_trade_records_csv 端到端（写入临时文件后读取）"""
        rows = [
            ["id", "strategy", "symbol", "order_id", "side", "order_type",
             "quantity", "price", "commission", "status", "executed_at",
             "realized_pnl", "close_reason", "algo_id"],
            ["1", "hrs", "BTRUSDT", "101", "SELL", "TAKE_PROFIT",
             "0.1", "98343", "0.01", "FILLED", "2026-09-01 08:30:00",
             "-9.8343", "", ""],
            ["2", "hrs", "BTRUSDT", "", "SELL", "PNL_SUMMARY",
             "0", "0", "0", "FILLED", "2026-09-01 10:00:00",
             "-20.5883", "", ""],
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
            w = csv.writer(f)
            for row in rows:
                w.writerow(row)
            path = f.name
        try:
            records = load_trade_records_csv(path)
            assert len(records) == 2
            assert records[0]["symbol"] == "BTRUSDT"
            assert records[0]["realized_pnl"] == Decimal("-9.8343")
            assert records[1]["is_summary"] is True
        finally:
            os.unlink(path)


# ======================================================================================
# 测试 4：Income 分片分页 + 完整性断言（mock BinanceClient）
# ======================================================================================

class TestIncomePagination:
    """fetch_income_sharded 分片分页行为"""

    def _make_mock_client(self, responses):
        """构造 mock BinanceClient，responses 为每次调用返回值列表。迭代器耗尽后返回空列表（安全终止）。"""
        client = MagicMock()
        client.close = AsyncMock()

        # 自定义 callable：耗尽后返回 [] 避免 StopAsyncIteration
        it = iter(list(responses))
        async def side_effect(**kwargs):
            try:
                return next(it)
            except StopIteration:
                return []

        client.get_income_history = AsyncMock(side_effect=side_effect)
        return client

    @pytest.mark.asyncio
    async def test_single_page_no_pagination(self, default_cfg):
        """返回条数 < limit → 不续拉，完成"""
        batch = [
            {"symbol": "BTCUSDT", "time": _MOCK_BASE_MS + i * 1000, "income": "1.0"}
            for i in range(3)  # 3 < batch_size=5
        ]
        client = self._make_mock_client([batch])

        with patch("ai_tuner.reconciler.hrs_income_reconcile.BinanceClient", return_value=client):
            result = await fetch_income_sharded(default_cfg)

        assert len(result.records) == 3
        assert result.truncated_chunks == []
        assert client.close.await_count == 1

    @pytest.mark.asyncio
    async def test_pagination_when_full_limit(self, default_cfg):
        """返回条数 == limit → 续拉下一页"""
        page1 = [
            {"symbol": "BTCUSDT", "time": _MOCK_BASE_MS + i * 1000, "income": "1.0"}
            for i in range(5)  # 5 == batch_size → 续拉
        ]
        page2 = [
            {"symbol": "BTCUSDT", "time": _MOCK_BASE_MS + 5000 + i * 1000, "income": "1.0"}
            for i in range(2)  # 2 < 5 → 停止
        ]
        client = self._make_mock_client([page1, page2])

        with patch("ai_tuner.reconciler.hrs_income_reconcile.BinanceClient", return_value=client):
            result = await fetch_income_sharded(default_cfg)

        assert len(result.records) == 7
        assert result.truncated_chunks == []
        # 关键验证：mock 预设了 page1（满 limit）→ page2（< limit），说明分页续拉正常完成
        # （总调用次数 = 36 chunk × 1-2 次/chunk，此处只验证数据正确 + 无截断即可）

    @pytest.mark.asyncio
    async def test_truncation_when_last_time_not_advance(self, default_cfg):
        """返回满 limit 但 last_time <= cursor → 标记截断"""
        # 所有记录 time 都相同（或 time 不递增），导致 last_time == cursor
        frozen_batch = [
            {"symbol": "BTCUSDT", "time": _MOCK_BASE_MS, "income": "1.0"}
            for _ in range(5)  # 满 limit，但 time 都相同
        ]
        client = self._make_mock_client([frozen_batch])

        with patch("ai_tuner.reconciler.hrs_income_reconcile.BinanceClient", return_value=client):
            result = await fetch_income_sharded(default_cfg)

        assert result.total_raw_count == 5
        assert len(result.truncated_chunks) >= 1

    @pytest.mark.asyncio
    async def test_empty_returns_zero_records(self, default_cfg):
        """全空响应 → 返回空列表"""
        client = self._make_mock_client([[]])

        with patch("ai_tuner.reconciler.hrs_income_reconcile.BinanceClient", return_value=client):
            result = await fetch_income_sharded(default_cfg)

        assert len(result.records) == 0

    @pytest.mark.asyncio
    async def test_multiple_chunks(self, default_cfg):
        """时间范围跨多个 chunk → 每个 chunk 独立拉取"""
        # 只模拟一个 chunk 的响应（函数会分多个 chunk，但我们只看参数传递）
        batch = [
            {"symbol": "BTCUSDT", "time": _MOCK_BASE_MS + i * 10000, "income": "1.0"}
            for i in range(3)
        ]
        # 返回足够多的空列表终止循环
        client = self._make_mock_client([batch] + [[] for _ in range(10)])

        with patch("ai_tuner.reconciler.hrs_income_reconcile.BinanceClient", return_value=client):
            result = await fetch_income_sharded(default_cfg)

        # chunk_count 至少 1，可能更多（取决于时间范围）
        assert result.chunk_count >= 1


# ======================================================================================
# 测试 5：逐笔匹配核心
# ======================================================================================

class TestMatchCore:
    """match_income_to_trades + _pick_best_candidate"""

    def test_time_window_filter(self, small_window_cfg):
        """超出时间窗的候选不匹配"""
        trade = _make_trade_record(
            exec_time=datetime(2026, 9, 1, 8, 31, 30),  # +90 秒
        )
        income = _make_income(
            time_ms=_bj_naive_to_utc_ms(datetime(2026, 9, 1, 8, 30, 0)),
        )
        results, summary = match_income_to_trades(
            [income], [trade], small_window_cfg,
        )
        assert len(results) == 1
        assert results[0].matched is False
        assert "无候选在容差内" in results[0].unresolved_reason or "同 symbol" in results[0].unresolved_reason

    def test_amount_tolerance(self, small_window_cfg):
        """金额差在容差内的匹配，超出则不匹配"""
        trade_in = _make_trade_record(pnl=Decimal("-9.8343"))
        trade_out = _make_trade_record(pnl=Decimal("-9.90"))
        income = _make_income(amount=Decimal("-9.8343"))

        # 容差 max(0.01, 9.8343*1%) = max(0.01, 0.098) = 0.098
        # -9.8343 vs -9.8343: diff=0 ✅
        # -9.8343 vs -9.90: diff=0.0657 <= 0.098 ✅（容差够）
        results, summary = match_income_to_trades(
            [income], [trade_in, trade_out], small_window_cfg,
        )
        assert results[0].matched is True

    def test_amount_outside_tolerance(self, small_window_cfg):
        """金额差超容差 → 不匹配"""
        trade = _make_trade_record(pnl=Decimal("-15.00"))  # 差太多
        income = _make_income(amount=Decimal("-9.8343"))

        results, summary = match_income_to_trades(
            [income], [trade], small_window_cfg,
        )
        assert results[0].matched is False

    def test_exit_order_preferred(self, small_window_cfg):
        """同时间窗同 symbol：平仓单（TAKE_PROFIT）优先于 LIMIT"""
        trade_exit = _make_trade_record(order_type="TAKE_PROFIT", pnl=Decimal("-9.8343"))
        trade_limit = _make_trade_record(order_type="LIMIT", pnl=Decimal("-9.8343"))
        income = _make_income(amount=Decimal("-9.8343"))

        # 两个候选金额相同，平仓单应优先胜出
        results, summary = match_income_to_trades(
            [income], [trade_exit, trade_limit], small_window_cfg,
        )
        assert results[0].matched is True
        assert results[0].matched_record_id == trade_exit["id"]

    def test_strategy_ambiguity_unresolved(self, small_window_cfg):
        """同 symbol 同时间窗同金额，hrs + new_coin 都有平仓单 → 歧义 → 未决"""
        trade_hrs = _make_trade_record(strategy="hrs", order_type="TAKE_PROFIT", pnl=Decimal("-5.00"))
        trade_new = _make_trade_record(strategy="new_coin", order_type="TAKE_PROFIT", pnl=Decimal("-5.00"))
        income = _make_income(amount=Decimal("-5.00"))

        results, summary = match_income_to_trades(
            [income], [trade_hrs, trade_new], small_window_cfg,
        )
        assert results[0].matched is False
        assert "歧义" in (results[0].unresolved_reason or "")

    def test_hrs_only_candidate(self, small_window_cfg):
        """同 symbol 只有 hrs 候选 → 正常匹配，归属 hrs"""
        trade_hrs = _make_trade_record(strategy="hrs", pnl=Decimal("-9.8343"))
        trade_other = _make_trade_record(strategy="new_coin", symbol="ETHUSDT", pnl=Decimal("-1.0"))
        income = _make_income(symbol="BTRUSDT", amount=Decimal("-9.8343"))

        results, summary = match_income_to_trades(
            [income], [trade_hrs, trade_other], small_window_cfg,
        )
        assert results[0].matched is True
        assert results[0].resolved_strategy == "hrs"

    def test_no_same_symbol(self, small_window_cfg):
        """income symbol 在 trade_records 里完全不存在 → 不匹配"""
        trade = _make_trade_record(symbol="ETHUSDT")
        income = _make_income(symbol="DOGEUSDT")
        results, summary = match_income_to_trades(
            [income], [trade], small_window_cfg,
        )
        assert results[0].matched is False
        assert summary.matched_count == 0

    def test_pnl_summary_not_matched(self, small_window_cfg):
        """PNL_SUMMARY 汇总行不参与匹配（避免双计）"""
        summary_trade = _make_trade_record(is_summary=True, pnl=Decimal("-20.5883"))
        detail_trade = _make_trade_record(pnl=Decimal("-9.8343"))
        income = _make_income(amount=Decimal("-9.8343"))

        results, summary = match_income_to_trades(
            [income], [summary_trade, detail_trade], small_window_cfg,
        )
        # 应匹配明细行，跳过汇总行
        assert results[0].matched is True
        assert results[0].matched_record_id == detail_trade["id"]

    def test_realized_pnl_none_not_matched(self, small_window_cfg):
        """trade_record realized_pnl=None → 不参与匹配"""
        trade_null = _make_trade_record(pnl=None)
        income = _make_income(amount=Decimal("-9.8343"))

        results, summary = match_income_to_trades(
            [income], [trade_null], small_window_cfg,
        )
        assert results[0].matched is False

    def test_duplicate_income_not_double_matched(self, small_window_cfg):
        """两笔 income 金额相同同一 symbol，不应匹配到同一 trade_record"""
        trade = _make_trade_record(pnl=Decimal("-9.8343"))
        inc1 = _make_income(amount=Decimal("-9.8343"), time_ms=_bj_naive_to_utc_ms(datetime(2026, 9, 1, 8, 30, 0)))
        inc2 = _make_income(amount=Decimal("-9.8343"), time_ms=_bj_naive_to_utc_ms(datetime(2026, 9, 1, 8, 30, 30)))

        results, summary = match_income_to_trades(
            [inc1, inc2], [trade], small_window_cfg,
        )
        assert results[0].matched is True
        assert results[1].matched is False  # 同 trade 已被第一笔占用
        assert summary.matched_count == 1


# ======================================================================================
# 测试 6：_pick_best_candidate 评分
# ======================================================================================

class TestPickBestCandidate:
    def test_exit_order_bonus(self):
        """平仓单在时间差稍大时也能胜出（加成）"""
        cfg = HRSReconcileConfig()
        qualified = [
            # LIMIT：diff=2000ms，平仓加成前 2000
            (_make_trade_record(record_id="1", order_type="LIMIT"), 2000, Decimal("0")),
            # STOP：diff=2500ms，平仓加成 -1000 → 1500
            (_make_trade_record(record_id="2", order_type="STOP"), 2500, Decimal("0")),
        ]
        result = _pick_best_candidate(qualified, 0, cfg)
        assert result is not None
        assert result[0]["id"] == "2"

    def test_ambiguity_return_none(self):
        """多策略歧义 → 返回 None"""
        cfg = HRSReconcileConfig()
        qualified = [
            (_make_trade_record(record_id="1", strategy="hrs", order_type="TAKE_PROFIT"), 100, Decimal("0")),
            (_make_trade_record(record_id="2", strategy="new_coin", order_type="TAKE_PROFIT"), 100, Decimal("0")),
        ]
        result = _pick_best_candidate(qualified, 0, cfg)
        assert result is None

    def test_empty_returns_none(self):
        cfg = HRSReconcileConfig()
        result = _pick_best_candidate([], 0, cfg)
        assert result is None


# ======================================================================================
# 测试 7：持仓级去重（口径 D）
# ======================================================================================

class TestPositionLevelPnl:
    def test_with_summary_only(self):
        """有 PNL_SUMMARY → 只计汇总"""
        recs = [
            _make_trade_record(record_id="d1", is_summary=True, pnl=Decimal("-20.00")),
            _make_trade_record(record_id="d2", pnl=Decimal("-10.00")),  # 明细被忽略
            _make_trade_record(record_id="d3", pnl=Decimal("-10.00")),  # 明细被忽略
        ]
        total = _compute_position_level_pnl(recs)
        assert total == Decimal("-20.00")

    def test_without_summary(self):
        """无 PNL_SUMMARY → 计全部明细"""
        recs = [
            _make_trade_record(record_id="d1", pnl=Decimal("-10.00")),
            _make_trade_record(record_id="d2", pnl=Decimal("-10.00")),
        ]
        total = _compute_position_level_pnl(recs)
        assert total == Decimal("-20.00")

    def test_mixed_symbols(self):
        """多 symbol 多 side 混合 → 分别聚合"""
        recs = [
            # BTRUSDT SELL：有汇总 → 计汇总
            _make_trade_record(record_id="d1", symbol="BTRUSDT", is_summary=True, pnl=Decimal("-20.00")),
            _make_trade_record(record_id="d2", symbol="BTRUSDT", pnl=Decimal("-10.00")),
            # ETHUSDT BUY：无汇总 → 计明细
            _make_trade_record(record_id="d3", symbol="ETHUSDT", side="BUY", pnl=Decimal("+5.00")),
            _make_trade_record(record_id="d4", symbol="ETHUSDT", side="BUY", pnl=Decimal("+3.00")),
        ]
        total = _compute_position_level_pnl(recs)
        assert total == Decimal("-12.00")  # -20 + 5 + 3

    def test_none_pnl_skipped(self):
        """realized_pnl=None 的记录跳过"""
        recs = [
            _make_trade_record(record_id="d1", pnl=None),
            _make_trade_record(record_id="d2", pnl=Decimal("-5.00")),
        ]
        total = _compute_position_level_pnl(recs)
        assert total == Decimal("-5.00")


# ======================================================================================
# 测试 8：真值表生成
# ======================================================================================

class TestMonthlyTruthTable:
    def test_five_calibres(self):
        """五口径基本计算：A 汇总 / B 明细 / C 混合 / D 持仓级 / E income"""
        # 时间全部在 2026-09
        sep_1 = datetime(2026, 9, 1, 8, 30, 0)
        sep_15 = datetime(2026, 9, 15, 10, 0, 0)
        sep_28 = datetime(2026, 9, 28, 9, 0, 0)

        trade_records = [
            # BTRUSDT：有汇总组
            _make_trade_record(record_id="1", symbol="BTRUSDT", order_type="TAKE_PROFIT", pnl=Decimal("-9.8343"), exec_time=sep_1),
            _make_trade_record(record_id="2", symbol="BTRUSDT", is_summary=True, pnl=Decimal("-20.5883"), exec_time=sep_1),
            # ETHUSDT：无汇总组（边界测试，持仓级应回退明细）
            _make_trade_record(record_id="3", symbol="ETHUSDT", order_type="STOP_MARKET", pnl=Decimal("+2.00"), exec_time=sep_15),
            # 10 月记录（不应进入 9 月真值）
            _make_trade_record(record_id="4", symbol="BTCUSDT", order_type="LIMIT", pnl=Decimal("-5.00"), exec_time=datetime(2026, 10, 3, 8, 0, 0)),
        ]

        # income：两条 BTRUSDT 被匹配，一条 ETHUSDT 被匹配
        inc_btr = _make_income(symbol="BTRUSDT", amount=Decimal("-9.8343"), time_ms=_bj_naive_to_utc_ms(sep_1) + 5000)
        inc_eth = _make_income(symbol="ETHUSDT", amount=Decimal("+2.00"), time_ms=_bj_naive_to_utc_ms(sep_15) + 5000)
        inc_unmatched = _make_income(symbol="BTCUSDT", amount=Decimal("-5.00"), time_ms=_bj_naive_to_utc_ms(datetime(2026, 10, 3, 8, 0, 0)))

        match_results = [
            MatchResult(income_symbol="BTRUSDT", income_time_ms=inc_btr["time"], income_amount=Decimal("-9.8343"),
                       matched=True, resolved_strategy="hrs", matched_record_id="1", time_diff_ms=5000),
            MatchResult(income_symbol="ETHUSDT", income_time_ms=inc_eth["time"], income_amount=Decimal("+2.00"),
                       matched=True, resolved_strategy="hrs", matched_record_id="3", time_diff_ms=5000),
            MatchResult(income_symbol="BTCUSDT", income_time_ms=inc_unmatched["time"], income_amount=Decimal("-5.00"),
                       matched=False, unresolved_reason="同 symbol 无可用 trade_records"),
        ]

        cfg = HRSReconcileConfig(start_date="2026-07-01")
        rows = compute_monthly_truth_table(
            trade_records, [inc_btr, inc_eth, inc_unmatched], match_results, cfg,
        )

        sep_row = next((r for r in rows if r.month == "2026-09"), None)
        assert sep_row is not None

        # 口径 A：仅汇总行 = -20.5883（BTRUSDT PNL_SUMMARY）
        assert sep_row.calibre_A_summary == Decimal("-20.5883")
        # 口径 B：仅明细行 = -9.8343 + 2.00 = -7.8343
        assert sep_row.calibre_B_detail == Decimal("-7.8343")
        # 口径 C：全部 = A + B = -28.4226
        assert sep_row.calibre_C_all == Decimal("-28.4226")
        # 口径 D：持仓级去重
        #   BTRUSDT(SELL) 有汇总 → -20.5883
        #   ETHUSDT(SELL) 无汇总 → +2.00
        #   合计 = -18.5883
        assert sep_row.calibre_D_position == Decimal("-18.5883")
        # 口径 E：income 逐笔匹配归属 HRS = -9.8343 + 2.00 = -7.8343
        assert sep_row.calibre_E_income_hrs == Decimal("-7.8343")

    def test_five_calibres_empty_month(self):
        """某月无数据 → 各口径均为 Decimal('0')"""
        cfg = HRSReconcileConfig(start_date="2026-07-01")
        rows = compute_monthly_truth_table([], [], [], cfg)
        # 可能无行，也可能有空行（取决于月份来源），但全是 0
        for r in rows:
            assert r.calibre_A_summary == Decimal("0")
            assert r.calibre_B_detail == Decimal("0")
            assert r.calibre_D_position == Decimal("0")


# ======================================================================================
# 测试 9：BTRUSDT 铁证验证（集成）
# ======================================================================================

class TestBTRUSDTVerification:
    """BTRUSDT 9/1 两笔（-9.8343/-10.7540）在 income 中的验证"""

    def test_both_found(self, small_window_cfg):
        """两笔 BTRUSDT 9/1 income 都找到 → btrusdt_verified=True"""
        sep1_morning = _bj_naive_to_utc_ms(datetime(2026, 9, 1, 8, 30, 15))
        sep1_noon = _bj_naive_to_utc_ms(datetime(2026, 9, 1, 12, 0, 15))

        trade1 = _make_trade_record(symbol="BTRUSDT", pnl=Decimal("-9.8343"), exec_time=datetime(2026, 9, 1, 8, 30, 0))
        trade2 = _make_trade_record(symbol="BTRUSDT", pnl=Decimal("-10.7540"), exec_time=datetime(2026, 9, 1, 12, 0, 0))
        inc1 = _make_income(symbol="BTRUSDT", amount=Decimal("-9.8343"), time_ms=sep1_morning)
        inc2 = _make_income(symbol="BTRUSDT", amount=Decimal("-10.7540"), time_ms=sep1_noon)

        results, summary = match_income_to_trades(
            [inc1, inc2], [trade1, trade2], small_window_cfg,
        )
        assert summary.btrusdt_verified is True
        assert all(m.matched for m in results)

    def test_partial_missing(self, small_window_cfg):
        """只找到一笔 → btrusdt_verified=False"""
        sep1_morning = _bj_naive_to_utc_ms(datetime(2026, 9, 1, 8, 30, 15))
        trade1 = _make_trade_record(symbol="BTRUSDT", pnl=Decimal("-9.8343"), exec_time=datetime(2026, 9, 1, 8, 30, 0))
        inc1 = _make_income(symbol="BTRUSDT", amount=Decimal("-9.8343"), time_ms=sep1_morning)
        # 第二笔用 BTCUSDT（错误 symbol）
        inc2 = _make_income(symbol="BTCUSDT", amount=Decimal("-10.7540"), time_ms=sep1_morning)

        results, summary = match_income_to_trades(
            [inc1, inc2], [trade1], small_window_cfg,
        )
        assert summary.btrusdt_verified is False


# ======================================================================================
# 测试 10：边界条件
# ======================================================================================

class TestEdgeCases:
    def test_zero_amount_income_skipped(self, small_window_cfg):
        """income amount=0 → 被跳过"""
        trade = _make_trade_record(pnl=Decimal("0"))
        income = _make_income(amount=Decimal("0"))
        results, summary = match_income_to_trades([income], [trade], small_window_cfg)
        # amount=0 被跳过，total_income_count 不计数
        assert summary.total_income_count == 0

    def test_empty_trade_records(self, small_window_cfg):
        """trade_records 全空 → 全部 income 不匹配"""
        income = _make_income(symbol="BTCUSDT")
        results, summary = match_income_to_trades([income], [], small_window_cfg)
        assert results[0].matched is False
        assert summary.matched_count == 0

    def test_empty_income(self, small_window_cfg):
        """income 全空 → 全部 matched_count=0"""
        trade = _make_trade_record()
        results, summary = match_income_to_trades([], [trade], small_window_cfg)
        assert summary.matched_count == 0
        assert summary.total_income_count == 0
