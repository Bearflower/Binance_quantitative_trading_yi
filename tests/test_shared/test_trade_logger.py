"""
止损打标（_mark_existing_close_record）单元测试

覆盖 realized_pnl=None / 有值、_parse_update_count 影响行数 >0 / =0、异常降级路径。
均通过 mock DatabaseManager 执行，不发起真实请求。
"""
from datetime import datetime, timedelta
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
        return TradeLogger(db, "测试策略", account_id="main"), db

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
        # 第八个参数限定到账户。
        assert len(params) == 9 and params[7] == "main"
        assert params[8].lower is None and params[8].upper is None
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
        assert len(params) == 9 and params[7] == "main"
        assert params[8].lower is None and params[8].upper is None
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
    async def test_since_none_retains_unconditional_insert(self):
        db = AsyncMock()
        logger = TradeLogger(db, "hrs", account_id="main")
        assert await logger.insert_pnl_summary(Decimal("-12.5"), "SUIUSDT", "SELL")
        db.execute.assert_awaited_once()
        db.execute_transaction.assert_not_awaited()

    async def test_episode_check_and_insert_share_one_transaction(self):
        db = AsyncMock()
        logger = TradeLogger(db, "hrs", account_id="main")
        since = datetime(2026, 8, 21, 11)
        assert await logger.insert_pnl_summary(Decimal("-12.5"), "SUIUSDT", "SELL", since=since)
        lock, insert = db.execute_transaction.await_args.args[0]
        assert "pg_advisory_xact_lock" in lock[0]
        assert "WHERE NOT EXISTS" in insert[0] and "executed_at >= $9" in insert[0]
        assert insert[1][7:9] == ("main", since)
        db.execute.assert_not_awaited()


class TestUpdateRealizedPnlFallbackGuard:
    """L1 修复：update_realized_pnl 降级匹配守卫
    - fallback SQL 排除 LIMIT/CONDITIONAL_ORDER 开仓单
    - 时间窗受 _MAX_FALLBACK_WINDOW_SECONDS 硬守卫约束
    """

    @pytest.fixture
    def logger(self):
        """构造 TradeLogger，mock DatabaseManager"""
        db = MagicMock()
        db.execute = AsyncMock(return_value="UPDATE 1")
        return TradeLogger(db, "测试策略", account_id="main"), db

    @pytest.mark.asyncio
    async def test_fallback_sql_excludes_limit_and_conditional(self, logger):
        """降级匹配 SQL 必须排除 LIMIT/CONDITIONAL_ORDER，防止错回填开仓单"""
        trade_logger, db = logger
        # order_id 传空字符串（falsey）→ 跳过模式一直接进入降级匹配
        ok = await trade_logger.update_realized_pnl(
            order_id="",
            symbol="BTCUSDT",
            side="SELL",
            realized_pnl=Decimal("-12.5"),
            executed_at=datetime(2026, 10, 14, 10, 0),
        )
        assert ok is True
        sql = db.execute.await_args.args[0]
        # 关键守卫：SELECT 里必须排除 LIMIT 和 CONDITIONAL_ORDER
        assert "order_type NOT IN ('LIMIT', 'CONDITIONAL_ORDER')" in sql

    @pytest.mark.asyncio
    async def test_time_window_capped_by_max_guard(self, logger):
        """传超大 time_window（86400s）时应被硬守卫截断到 _MAX_FALLBACK_WINDOW_SECONDS（默认 1800s）"""
        trade_logger, db = logger
        executed_at = datetime(2026, 10, 14, 10, 0)
        ok = await trade_logger.update_realized_pnl(
            order_id="",
            symbol="BTCUSDT",
            side="SELL",
            realized_pnl=Decimal("-12.5"),
            executed_at=executed_at,
            time_window=86400,  # 超大值
        )
        assert ok is True
        # 参数顺序：(sql, realized_pnl, strategy_name, symbol, match_side, start_time, end_time)
        start_time, end_time = db.execute.await_args.args[5], db.execute.await_args.args[6]
        expected_start = str(executed_at - timedelta(seconds=1800))
        expected_end = str(executed_at + timedelta(seconds=1800))
        assert str(start_time) == expected_start, f"期望 start={expected_start}，实际={start_time}"
        assert str(end_time) == expected_end, f"期望 end={expected_end}，实际={end_time}"

    @pytest.mark.asyncio
    async def test_small_window_passthrough(self, logger):
        """time_window=300s（5min，小于上限）不应被截断"""
        trade_logger, db = logger
        executed_at = datetime(2026, 10, 14, 10, 0)
        ok = await trade_logger.update_realized_pnl(
            order_id="",
            symbol="ETHUSDT",
            side="BUY",
            realized_pnl=Decimal("25.0"),
            executed_at=executed_at,
            time_window=300,  # 默认 5min
        )
        assert ok is True
        start_time, end_time = db.execute.await_args.args[5], db.execute.await_args.args[6]
        expected_start = str(executed_at - timedelta(seconds=300))
        expected_end = str(executed_at + timedelta(seconds=300))
        assert str(start_time) == expected_start, f"期望 start={expected_start}"
        assert str(end_time) == expected_end, f"期望 end={expected_end}"