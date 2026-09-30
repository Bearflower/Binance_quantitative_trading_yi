"""
止损打标（_mark_existing_close_record）单元测试

覆盖 realized_pnl=None / 有值、_parse_update_count 影响行数 >0 / =0、异常降级路径。
均通过 mock DatabaseManager 执行，不发起真实请求。
"""
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