"""新成交记录必须携带明确账户身份，历史空值不得被猜测回填。"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.trade_logger import TradeLogger


def test_live_logger_uses_compose_account_identity(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "pm-primary")
    assert TradeLogger(MagicMock(), "btc_eth").account_id == "pm-primary"


@pytest.mark.asyncio
async def test_order_insert_records_explicit_account_id():
    db = MagicMock()
    db.execute = AsyncMock(return_value="INSERT 0 1")
    logger = TradeLogger(db, "hrs", account_id="main")
    ok = await logger.log_order(
        {"orderId": 101, "status": "FILLED", "avgPrice": "10", "executedQty": "2"},
        "ABCUSDT", "BUY", "MARKET",
    )
    assert ok is True
    sql, *params = db.execute.await_args.args
    assert "account_id" in sql
    assert "main" in params


@pytest.mark.asyncio
async def test_conditional_order_insert_records_same_account_id():
    db = MagicMock()
    db.execute = AsyncMock(return_value="INSERT 0 1")
    logger = TradeLogger(db, "hrs", account_id="main")
    ok = await logger.log_order(
        {"algoId": 88, "status": "NEW"}, "ABCUSDT", "SELL", "STOP_MARKET",
    )
    assert ok is True
    sql, *params = db.execute.await_args.args
    assert "account_id" in sql
    assert params[-1] == "main"


@pytest.mark.asyncio
async def test_pnl_summary_records_same_account_id():
    db = MagicMock()
    db.execute = AsyncMock(return_value="INSERT 0 1")
    logger = TradeLogger(db, "hrs", account_id="main")
    ok = await logger.insert_pnl_summary(
        realized_pnl=Decimal("1.5"), symbol="ABCUSDT", side="BUY",
    )
    assert ok is True
    sql, *params = db.execute.await_args.args
    assert "account_id" in sql
    assert params[-1] == "main"


@pytest.mark.asyncio
async def test_pnl_summary_persists_explicit_source_and_defaults_unknown():
    db = MagicMock()
    db.execute = AsyncMock(return_value="INSERT 0 1")
    logger = TradeLogger(db, "hrs", account_id="main")
    assert await logger.insert_pnl_summary(
        realized_pnl=Decimal("1.5"), symbol="ABCUSDT", side="BUY",
        pnl_source="theoretical_estimate",
    )
    sql, *params = db.execute.await_args.args
    assert "pnl_source" in sql
    assert params[-2:] == ["theoretical_estimate", "main"]
    assert await logger.insert_pnl_summary(
        realized_pnl=Decimal("1.5"), symbol="ABCUSDT", side="BUY",
    )
    assert db.execute.await_args.args[-2] == "unknown"


@pytest.mark.asyncio
async def test_pnl_summary_rejects_unsupported_source_before_database_write():
    db = MagicMock()
    db.execute = AsyncMock()
    logger = TradeLogger(db, "hrs", account_id="main")
    with pytest.raises(ValueError, match="PnL 来源"):
        await logger.insert_pnl_summary(
            realized_pnl=Decimal("1"), symbol="ABCUSDT", side="BUY",
            pnl_source="verified_net_return",
        )
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_schema_adds_nullable_account_id_without_backfill():
    db = MagicMock()
    db.execute_ddl = AsyncMock()
    logger = TradeLogger(db, "hrs", account_id="main")
    await logger.ensure_table_exists()
    ddls = [call.args[0] for call in db.execute_ddl.await_args_list]
    assert any("ADD COLUMN IF NOT EXISTS account_id" in sql for sql in ddls)
    assert any("ADD COLUMN IF NOT EXISTS pnl_source" in sql for sql in ddls)
    assert not any("UPDATE trading.trade_records" in sql for sql in ddls)
