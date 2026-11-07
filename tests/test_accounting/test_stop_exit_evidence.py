"""A triggered conditional order is not a verified strategy exit."""

import asyncio
from unittest.mock import AsyncMock

from shared.accounting.stop_exit_evidence import (
    fetch_verified_stop_execution,
    verified_stop_execution,
)


def sample():
    algo = dict(algoId=11, symbol="SOLUSDT", orderType="STOP",
                algoStatus="FINISHED", actualOrderId="22", side="SELL")
    order = dict(orderId=22, symbol="SOLUSDT", status="FILLED",
                 side="SELL", reduceOnly=True, executedQty="0.3")
    fills = [dict(id=101, orderId=22, symbol="SOLUSDT", side="SELL",
                  qty="0.1", realizedPnl="-1.0", commission="0.01", commissionAsset="USDT", time=1000),
             dict(id=102, orderId=22, symbol="SOLUSDT", side="SELL",
                  qty="0.2", realizedPnl="-2.0", commission="0.02", commissionAsset="USDT", time=1001)]
    return algo, order, fills


def test_exact_stop_order_and_fills_are_auditable_without_lot_assignment():
    algo, order, fills = sample()

    event = verified_stop_execution("default", "SOLUSDT", 11, algo, order, fills)

    assert event == {
        "account_id": "default", "symbol": "SOLUSDT", "algo_id": 11,
        "order_id": 22, "trade_ids": [101, 102], "side": "SELL",
        "quantity": "0.3", "realized_pnl": "-3.0", "commission": "0.03",
        "commission_asset": "USDT",
        "last_fill_time_ms": 1001,
    }


def test_trigger_without_actual_fill_is_not_an_exit():
    algo, order, fills = sample()
    algo["actualOrderId"] = ""
    assert verified_stop_execution("default", "SOLUSDT", 11, algo, order, fills) is None


def test_order_id_or_reduce_only_mismatch_is_not_an_exit():
    algo, order, fills = sample()
    order["reduceOnly"] = False
    assert verified_stop_execution("default", "SOLUSDT", 11, algo, order, fills) is None
    order["reduceOnly"] = True
    fills[1]["orderId"] = 23
    assert verified_stop_execution("default", "SOLUSDT", 11, algo, order, fills) is None


def test_partial_or_duplicate_fills_are_not_full_exit_evidence():
    algo, order, fills = sample()
    fills[1]["id"] = 101
    assert verified_stop_execution("default", "SOLUSDT", 11, algo, order, fills) is None


def test_fetch_requires_exact_exchange_order_and_fills():
    algo, order, fills = sample()
    client = type("Client", (), {})()
    client.get_algo_order = AsyncMock(return_value=algo)
    client.get_order = AsyncMock(return_value=order)
    client.get_user_trades = AsyncMock(return_value=fills)

    event = asyncio.run(fetch_verified_stop_execution(client, "default", "SOLUSDT", 11))

    assert event["order_id"] == 22
    client.get_order.assert_awaited_once_with("SOLUSDT", 22)
    client.get_user_trades.assert_awaited_once_with("SOLUSDT", order_id="22", limit=1000)


def test_fetch_does_not_read_trades_without_actual_order_id():
    algo, _, _ = sample()
    algo["actualOrderId"] = ""
    client = type("Client", (), {})()
    client.get_algo_order = AsyncMock(return_value=algo)
    client.get_order = AsyncMock()
    client.get_user_trades = AsyncMock()

    assert asyncio.run(fetch_verified_stop_execution(client, "default", "SOLUSDT", 11)) is None
    client.get_order.assert_not_awaited()
    client.get_user_trades.assert_not_awaited()


def test_fetch_rejects_full_page_without_proven_end():
    algo, order, fills = sample()
    client = type("Client", (), {})()
    client.get_algo_order = AsyncMock(return_value=algo)
    client.get_order = AsyncMock(return_value=order)
    client.get_user_trades = AsyncMock(return_value=fills * 500)

    assert asyncio.run(fetch_verified_stop_execution(client, "default", "SOLUSDT", 11)) is None
    fills[1]["id"] = 102
    order["executedQty"] = "0.4"
    assert verified_stop_execution("default", "SOLUSDT", 11, algo, order, fills) is None
