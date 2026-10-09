"""
止损打标（_mark_existing_close_record）单元测试

覆盖 realized_pnl=None / 有值、_parse_update_count 影响行数 >0 / =0、异常降级路径。
均通过 mock DatabaseManager 执行，不发起真实请求。
"""
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.trade_logger import TradeLogger


class TestMarkExistingCloseRecord:
    """测试 TradeLogger._mark_existing_close_record 止损打标"""

    @pytest.fixture
    def logger(self):
        """构造 TradeLogger，mock DatabaseManager"""
        db = MagicMock()
        db.execute = AsyncMock(return_value="UPDATE 1")
        return TradeLogger(db, "测试策略"), db

    @pytest.mark.asyncio
    async def test_realized_pnl_none_keeps_original(self, logger):
        """realized_pnl=None 时应以 COALESCE 保持原值，且 $2 恒存在"""
        trade_logger, db = logger

        ok = await trade_logger._mark_existing_close_record(
            strategy_name="测试策略", symbol="BTCUSDT", side="SELL",
            realized_pnl=None, executed_at=None,
        )

        assert ok is True
        sql = db.execute.await_args.args[0]
        # 固定 7 占位符，不再条件拼接
        assert "COALESCE($2::numeric, realized_pnl)" in sql
        # 旧的"条件拼接 $2"模式必须消失，防止回归
        assert ", realized_pnl = $2" not in sql
        params = db.execute.await_args.args[1:]
        # 参数列表固定 7 个：$1..$7
        assert len(params) == 7
        # $2 显式传 None
        assert params[1] is None

    @pytest.mark.asyncio
    async def test_realized_pnl_with_value(self, logger):
        """realized_pnl 有值时传入字符串，覆盖原值"""
        trade_logger, db = logger

        ok = await trade_logger._mark_existing_close_record(
            strategy_name="测试策略", symbol="BTCUSDT", side="SELL",
            realized_pnl=Decimal("-12.5"), executed_at=None,
        )

        assert ok is True
        params = db.execute.await_args.args[1:]
        assert len(params) == 7
        assert params[1] == "-12.5"

    @pytest.mark.asyncio
    async def test_zero_rows_affected_returns_false(self, logger):
        """影响行数为 0（未命中真实平仓记录）应返回 False"""
        trade_logger, db = logger
        db.execute = AsyncMock(return_value="UPDATE 0")

        ok = await trade_logger._mark_existing_close_record(
            strategy_name="测试策略", symbol="BTCUSDT", side="SELL",
            realized_pnl=None, executed_at=None,
        )

        assert ok is False

    @pytest.mark.asyncio
    async def test_exception_degrades_to_false(self, logger):
        """db.execute 抛异常时降级返回 False，不向上抛"""
        trade_logger, db = logger
        db.execute = AsyncMock(side_effect=RuntimeError("连接失败"))

        ok = await trade_logger._mark_existing_close_record(
            strategy_name="测试策略", symbol="BTCUSDT", side="SELL",
            realized_pnl=None, executed_at=None,
        )

        assert ok is False


class TestInsertPnlSummaryIdempotency:
    """测试 TradeLogger.insert_pnl_summary 的 since 幂等去重

    回归背景：HRS 整笔平仓回写的是「自开仓起的累计盈亏」，非幂等——进程重启或
    监控循环重复命中同一次平仓时，会用完全相同的金额再写一条 PNL_SUMMARY，
    导致同一笔平仓被重复计入 realized_pnl。
    """

    @pytest.fixture
    def logger(self):
        db = MagicMock()
        db.execute = AsyncMock(return_value="INSERT 0 1")
        db.fetch_one = AsyncMock(return_value=None)
        return TradeLogger(db, "hrs"), db

    @pytest.mark.asyncio
    async def test_since_none_skips_dedup_check(self, logger):
        """since=None 时不做去重查询，保持原有行为（直接插入）"""
        trade_logger, db = logger

        ok = await trade_logger.insert_pnl_summary(
            realized_pnl=Decimal("-12.5"), symbol="SUIUSDT", side="SELL",
        )

        assert ok is True
        db.fetch_one.assert_not_awaited()
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_since_hits_existing_skips_insert(self, logger):
        """since 非 None 且已存在同额 PNL_SUMMARY → 跳过插入并返回 True"""
        trade_logger, db = logger
        db.fetch_one = AsyncMock(return_value={"id": 123})

        ok = await trade_logger.insert_pnl_summary(
            realized_pnl=Decimal("-12.5"), symbol="SUIUSDT", side="SELL",
            since=datetime(2026, 8, 21, 11, 0),
        )

        assert ok is True
        db.fetch_one.assert_awaited_once()
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_since_misses_inserts(self, logger):
        """since 非 None 但无同额记录 → 正常插入"""
        trade_logger, db = logger
        db.fetch_one = AsyncMock(return_value=None)

        ok = await trade_logger.insert_pnl_summary(
            realized_pnl=Decimal("-12.5"), symbol="SUIUSDT", side="SELL",
            since=datetime(2026, 8, 21, 11, 0),
        )

        assert ok is True
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dedup_query_scope_and_params(self, logger):
        """去重查询必须限定 PNL_SUMMARY、同额，并以 since 为下界"""
        trade_logger, db = logger
        db.fetch_one = AsyncMock(return_value=None)
        since = datetime(2026, 8, 21, 11, 0)

        await trade_logger.insert_pnl_summary(
            realized_pnl=Decimal("-12.5"), symbol="SUIUSDT", side="SELL",
            since=since,
        )

        sql, *params = db.fetch_one.await_args.args
        assert "order_type = 'PNL_SUMMARY'" in sql
        assert "realized_pnl = $4" in sql
        assert "executed_at >= $5" in sql
        assert params == ["hrs", "SUIUSDT", "SELL", Decimal("-12.5"), since]