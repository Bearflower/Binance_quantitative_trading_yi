"""
佣金回填功能单元测试

覆盖 BinanceClient.get_user_trades（封装 userTrades 端点）与
TradeLogger.reconcile_commissions（事后回填真实佣金）的公共能力。
均通过 mock BinanceClient._request / mock DatabaseManager 执行，不发起真实请求。
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.binance_api import BinanceClient
from shared.trade_logger import TradeLogger


def _make_client(use_unified_account: bool) -> BinanceClient:
    """构造测试用 BinanceClient（不建立真实会话）"""
    return BinanceClient(
        api_key="test_api_key_12345",
        api_secret="test_api_secret_67890",
        use_unified_account=use_unified_account,
    )


class TestGetUserTrades:
    """测试 BinanceClient.get_user_trades 端点封装"""

    @pytest.mark.asyncio
    async def test_pm_account_endpoint(self):
        """PM（统一）账户应请求 /papi/v1/um/userTrades"""

        client = _make_client(use_unified_account=True)
        client._request = AsyncMock(return_value=[{"symbol": "BTCUSDT", "orderId": "100"}])

        trades = await client.get_user_trades("BTCUSDT", order_id="100")

        assert trades == [{"symbol": "BTCUSDT", "orderId": "100"}]
        _, endpoint, params = client._request.await_args.args
        assert endpoint == "/papi/v1/um/userTrades"
        assert params == {"symbol": "BTCUSDT", "orderId": "100"}
        assert client._request.await_args.kwargs.get("signed") is True

    @pytest.mark.asyncio
    async def test_regular_account_endpoint(self):
        """普通合约账户应请求 /fapi/v1/userTrades"""

        client = _make_client(use_unified_account=False)
        client._request = AsyncMock(return_value=[])

        trades = await client.get_user_trades("BTCUSDT")

        assert trades == []
        _, endpoint, _ = client._request.await_args.args
        assert endpoint == "/fapi/v1/userTrades"

    @pytest.mark.asyncio
    async def test_symbol_uppercase_and_start_time_param(self):
        """交易对应转为大写，且透传 start_time 参数"""

        client = _make_client(use_unified_account=True)
        client._request = AsyncMock(return_value=[])

        await client.get_user_trades("btcusdt", start_time=1700000000000)

        _, _, params = client._request.await_args.args
        assert params == {"symbol": "BTCUSDT", "startTime": 1700000000000}

    @pytest.mark.asyncio
    async def test_empty_symbol_raises(self):
        """空交易对应抛 ValueError"""

        client = _make_client(use_unified_account=True)
        with pytest.raises(ValueError, match="交易对不能为空"):
            await client.get_user_trades("   ")

    @pytest.mark.asyncio
    async def test_non_list_response_returns_empty(self):
        """非列表响应应回退为空列表，不抛异常"""

        client = _make_client(use_unified_account=True)
        client._request = AsyncMock(return_value={"error": "bad"})

        trades = await client.get_user_trades("BTCUSDT")

        assert trades == []


class TestReconcileCommissions:
    """测试 TradeLogger.reconcile_commissions 佣金回填"""

    @pytest.fixture
    def logger(self):
        """构造 TradeLogger，mock DatabaseManager"""
        db = MagicMock()
        db.fetch_all = AsyncMock(return_value=[])
        db.execute = AsyncMock(return_value="UPDATE 0")
        return TradeLogger(db, "测试策略"), db

    @pytest.mark.asyncio
    async def test_normal_backfill(self):
        """正常回填：累加该订单全部成交佣金并写库"""

        trade_logger, db = (self._build_logger_with_pending(
            [{"symbol": "BTCUSDT", "order_id": "100"}]
        ))
        binance_client = MagicMock()
        binance_client.get_user_trades = AsyncMock(return_value=[
            {"commission": "0.5"}, {"commission": "0.3"},
        ])

        result = await trade_logger.reconcile_commissions(binance_client, lookback_hours=24)

        assert result["queried_orders"] == 1
        assert result["matched_orders"] == 1
        assert result["total_commission"] == Decimal("-0.8")
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_only_update_still_zero_rows(self):
        """回填 UPDATE 必须限定 commission=0 的行（幂等）"""

        trade_logger, db = (self._build_logger_with_pending(
            [{"symbol": "BTCUSDT", "order_id": "100"}]
        ))
        binance_client = MagicMock()
        binance_client.get_user_trades = AsyncMock(return_value=[{"commission": "0.2"}])

        await trade_logger.reconcile_commissions(binance_client, lookback_hours=24)

        sql = db.execute.await_args.args[0]
        assert "commission = 0" in sql

    @pytest.mark.asyncio
    async def test_no_pending_records(self):
        """无待回填记录：不查询 userTrades 也不写库"""

        trade_logger, db = await self._make_logger()
        binance_client = MagicMock()
        binance_client.get_user_trades = AsyncMock(return_value=[{"commission": "0.1"}])

        result = await trade_logger.reconcile_commissions(binance_client, lookback_hours=24)

        assert result == {
            "queried_orders": 0,
            "matched_orders": 0,
            "total_commission": Decimal("0"),
        }
        binance_client.get_user_trades.assert_not_awaited()
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_order_id_skipped(self):
        """order_id 为空（如条件单记录）应跳过查询"""

        trade_logger, db = (self._build_logger_with_pending(
            [{"symbol": "BTCUSDT", "order_id": None}]
        ))
        binance_client = MagicMock()
        binance_client.get_user_trades = AsyncMock(return_value=[{"commission": "0.1"}])

        result = await trade_logger.reconcile_commissions(binance_client, lookback_hours=24)

        assert result["queried_orders"] == 1
        assert result["matched_orders"] == 0
        binance_client.get_user_trades.assert_not_awaited()
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_user_trades_exception_swallowed(self):
        """查 userTrades 抛异常时回退不报错，不影响整体"""

        trade_logger, db = (self._build_logger_with_pending(
            [{"symbol": "BTCUSDT", "order_id": "100"}]
        ))
        binance_client = MagicMock()
        binance_client.get_user_trades = AsyncMock(side_effect=RuntimeError("连接失败"))

        result = await trade_logger.reconcile_commissions(binance_client, lookback_hours=24)

        assert result["matched_orders"] == 0
        assert result["total_commission"] == Decimal("0")
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_commission_not_overwrite(self):
        """零佣金（sum=0）不应覆盖，不触发写库"""

        trade_logger, db = (self._build_logger_with_pending(
            [{"symbol": "BTCUSDT", "order_id": "100"}]
        ))
        binance_client = MagicMock()
        binance_client.get_user_trades = AsyncMock(return_value=[
            {"commission": "0"}, {"commission": ""},
        ])

        result = await trade_logger.reconcile_commissions(binance_client, lookback_hours=24)

        assert result["matched_orders"] == 0
        assert result["total_commission"] == Decimal("0")
        db.execute.assert_not_awaited()

    async def _make_logger(self):
        """返回 (TradeLogger, mocked db)，fetch_all 返回空"""
        db = MagicMock()
        db.fetch_all = AsyncMock(return_value=[])
        db.execute = AsyncMock(return_value="UPDATE 0")
        return TradeLogger(db, "测试策略"), db

    def _build_logger_with_pending(self, rows):
        """返回 (TradeLogger, mocked db)，fetch_all 返回指定待回填记录"""
        db = MagicMock()
        db.fetch_all = AsyncMock(return_value=rows)
        db.execute = AsyncMock(return_value="UPDATE 1")
        return TradeLogger(db, "测试策略"), db