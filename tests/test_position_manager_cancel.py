"""HRS 平仓清场只撤销属于本策略、且仍在交易所的父单。"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from strategies.hrs.position_manager import PositionManager


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "main")
    monkeypatch.setenv("BINANCE_CLASSIC_CUTOVER_MS", "1000")
    api = MagicMock()
    api.use_unified_account = True
    api.trade_logger.account_id = "main"
    api.trade_logger.strategy_name = "hrs"
    api.cancel_algo_order = AsyncMock()
    orders, receipts = [], []
    for oid, strategy in [(111, "hrs"), (222, "hrs"), (333, "btc_eth")]:
        orders.append(dict(symbol="XUSDT", algoId=oid, side="BUY", orderType="STOP_MARKET", createTime=999))
        receipts.append(dict(symbol="XUSDT", algo_id=str(oid), side="BUY", strategy=strategy,
                             created_at=datetime(1970, 1, 1), order_type="STOP_LOSS", tp_level=None))
    api.get_open_algo_orders = AsyncMock(return_value=orders)
    api.trade_logger.db.fetch_all = AsyncMock(return_value=receipts)
    manager = object.__new__(PositionManager)
    manager.binance_api = api
    manager._positions = {"XUSDT": {"algo_ids": {"sl":111}}}
    return manager


@pytest.mark.asyncio
async def test_owned_tracked_and_untracked_receipts_are_canceled(manager):
    result = await manager.cancel_all_orders("XUSDT")
    assert result["algo_ids"] == [111,222]
    assert manager.get_algo_ids("XUSDT") == []
    assert manager.binance_api.cancel_algo_order.await_args_list == [call("XUSDT",111),call("XUSDT",222)]
    manager.binance_api.cancel_all_algo_orders.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_receipt","unreachable","partial","old_mode"])
async def test_failed_cleanup_keeps_local_roles(manager, case):
    api = manager.binance_api
    if case == "missing_receipt":
        api.trade_logger.db.fetch_all.return_value.pop()
    elif case == "unreachable":
        api.get_open_algo_orders.side_effect = RuntimeError("offline")
    elif case == "old_mode":
        api.use_unified_account = False
    else:
        api.cancel_algo_order.side_effect = [RuntimeError("offline"), {"code":200}]
    result = await manager.cancel_all_orders("XUSDT")
    assert result["failed"] == 1
    assert result["cancelled"] == (1 if case == "partial" else 0)
    assert manager.get_algo_ids("XUSDT") == [111]
    api.cancel_all_algo_orders.assert_not_called()
