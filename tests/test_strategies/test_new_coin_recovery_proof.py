"""Exact exchange facts are required before a recovered lot gets a time exit."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.new_coin.recovery_evidence import verify_unreduced_short
from tests.test_strategies.test_take_profit_fill_detect import create_executor


OPENED = datetime(2026, 11, 11, 8, tzinfo=timezone.utc)
NOW = OPENED + timedelta(hours=2)
ENTRY = {"symbol": "PFEUSDT", "orderId": 141970, "side": "SELL",
         "status": "FILLED", "executedQty": "3.28", "avgPrice": "30.41271340"}
FILL = {"symbol": "PFEUSDT", "id": 9001, "orderId": 141970,
        "side": "SELL", "qty": "3.28", "time": int(OPENED.timestamp() * 1000)}
TP1 = {"symbol": "PFEUSDT", "algoId": 2000001554798281,
       "side": "BUY", "orderType": "TAKE_PROFIT", "algoStatus": "NEW",
       "actualOrderId": "", "reduceOnly": True, "quantity": "0.98"}
STOP = {"symbol": "PFEUSDT", "algoId": 2000001554798282,
        "side": "BUY", "orderType": "STOP", "algoStatus": "NEW",
        "reduceOnly": True, "quantity": "3.28", "triggerPrice": "33"}


def exchange():
    api = AsyncMock()
    api.get_order.return_value = dict(ENTRY)
    api.get_user_trades.side_effect = [[dict(FILL)], [dict(FILL)]]
    api.get_algo_order.return_value = dict(TP1)
    api.get_open_orders.return_value = []
    api.get_open_algo_orders.return_value = [dict(TP1), dict(STOP)]
    api.get_position.return_value = [{"symbol": "PFEUSDT", "positionAmt": "-3.28",
                                      "entryPrice": "30.41271340", "markPrice": "30"}]
    return api


async def verify(api, **overrides):
    args = dict(symbol="PFEUSDT", entry_order_id=141970,
                tp1_algo_id=2000001554798281, opened_at=OPENED,
                anchor=Decimal("3.28"), entry_price=Decimal("30.41271340"),
                remaining=Decimal("3.28"), now=NOW)
    args.update(overrides)
    return await verify_unreduced_short(api, **args)


@pytest.mark.asyncio
async def test_complete_unreduced_chain_and_active_protection_can_be_verified():
    assert await verify(exchange()) is True


@pytest.mark.asyncio
async def test_exchange_close_position_stop_covers_full_lot():
    api = exchange()
    api.get_open_algo_orders.return_value = [dict(TP1),
        {**STOP, "closePosition": True, "reduceOnly": False, "quantity": "0"}]
    assert await verify(api) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("gap", ["other_trade", "full_page", "tp1_triggered",
                                     "missing_stop", "ordinary_order", "wrong_position",
                                     "stale", "wrong_entry", "missing_tp1"])
async def test_gap_or_mixed_batch_cannot_unlock(gap):
    api = exchange()
    overrides = {}
    if gap == "other_trade":
        api.get_user_trades.side_effect = [[dict(FILL)], [dict(FILL),
            {**FILL, "id": 9002, "orderId": 777, "side": "BUY", "qty": "1"}]]
    elif gap == "full_page":
        api.get_user_trades.side_effect = [[dict(FILL)] * 1000]
    elif gap == "tp1_triggered":
        api.get_algo_order.return_value = {**TP1, "algoStatus": "FINISHED",
                                           "actualOrderId": "678821"}
    elif gap == "missing_stop":
        api.get_open_algo_orders.return_value = [dict(TP1)]
    elif gap == "ordinary_order":
        api.get_open_orders.return_value = [{"symbol": "PFEUSDT"}]
    elif gap == "wrong_position":
        api.get_position.return_value = [{"symbol": "PFEUSDT", "positionAmt": "-3.27",
                                          "entryPrice": "30.41271340", "markPrice": "30"}]
    elif gap == "stale":
        overrides["now"] = OPENED + timedelta(days=7)
    elif gap == "wrong_entry":
        api.get_order.return_value = {**ENTRY, "orderId": 141971}
    elif gap == "missing_tp1":
        api.get_open_algo_orders.return_value = [dict(STOP)]
    assert await verify(api, **overrides) is False


@pytest.mark.asyncio
async def test_recovered_time_exit_rechecks_exchange_proof_before_close(monkeypatch):
    executor = create_executor()
    executor.binance_api = exchange()
    executor.max_holding_hours = 1
    executor._close_position = AsyncMock(return_value=True)
    executor.notification = AsyncMock()
    opened = datetime.now(timezone.utc) - timedelta(hours=2)
    executor.position_tracking["PFEUSDT"] = {
        "time_exit_status": "review_required", "recovery_position_id": "PFEUSDT_141970",
        "recovery_entry_order_id": 141970, "recovery_tp1_algo_id": 2000001554798281,
        "tp_anchor_quantity": 3.28, "remaining_quantity": 3.28,
        "entry_price": 30.41271340, "target1_reached": False,
    }
    proof = AsyncMock(return_value=False)
    monkeypatch.setattr("strategies.new_coin.executor.verify_unreduced_short", proof,
                        raising=False)
    await executor._check_time_stop("PFEUSDT", opened)
    proof.assert_awaited_once()
    executor._close_position.assert_not_awaited()
    proof.reset_mock()
    proof.return_value = True
    await executor._check_time_stop("PFEUSDT", opened)
    proof.assert_awaited_once()
    executor._close_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_time_close_keeps_recovered_tracking(monkeypatch):
    executor = create_executor()
    executor.max_holding_hours = 1
    executor._close_position = AsyncMock(return_value=False)
    executor.notification = AsyncMock()
    opened = datetime.now(timezone.utc) - timedelta(hours=2)
    executor.position_tracking["PFEUSDT"] = {
        "time_exit_status": "review_required", "recovery_position_id": "PFEUSDT_141970",
        "recovery_entry_order_id": 141970, "recovery_tp1_algo_id": 2000001554798281,
        "tp_anchor_quantity": 3.28, "remaining_quantity": 3.28,
        "entry_price": 30.41271340, "target1_reached": False,
    }
    monkeypatch.setattr("strategies.new_coin.executor.verify_unreduced_short",
                        AsyncMock(return_value=True))

    await executor._check_time_stop("PFEUSDT", opened)

    assert "PFEUSDT" in executor.position_tracking
    executor.notification.send.assert_not_awaited()
