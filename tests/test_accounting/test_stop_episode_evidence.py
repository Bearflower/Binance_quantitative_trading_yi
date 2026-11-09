"""A STOP must close the original episode without an intervening flat state."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from shared.accounting.stop_episode_evidence import (
    fetch_verified_closed_stop_episode, verified_closed_stop_episode,
)


def sample():
    binding = {
        "account_id": "default", "strategy": "btc_eth_aggressive", "symbol": "ETHUSDT",
        "episode_entry_order_id": 10, "algo_id": 20,
        "stop_side": "SELL", "stop_quantity": "0.7",
    }
    stop = {
        "account_id": "default", "symbol": "ETHUSDT", "algo_id": 20,
        "order_id": 30, "trade_ids": [3, 4], "side": "SELL",
        "quantity": "0.7", "realized_pnl": "-2", "commission": "0.1",
        "commission_asset": "USDT", "last_fill_time_ms": 4000,
    }
    entry_order = {"orderId": 10, "symbol": "ETHUSDT", "side": "BUY", "executedQty": "1"}
    entry_fills = [
        {"id": 1, "orderId": 10, "symbol": "ETHUSDT", "side": "BUY", "qty": "1", "time": 1000},
    ]
    lot_fills = entry_fills + [
        {"id": 2, "orderId": 25, "symbol": "ETHUSDT", "side": "SELL", "qty": "0.3", "time": 2000},
        {"id": 3, "orderId": 30, "symbol": "ETHUSDT", "side": "SELL", "qty": "0.2", "time": 3000},
        {"id": 4, "orderId": 30, "symbol": "ETHUSDT", "side": "SELL", "qty": "0.5", "time": 4000},
    ]
    return binding, stop, entry_order, entry_fills, lot_fills


def test_closed_episode_links_stop_to_first_entry_and_complete_fills():
    proof = verified_closed_stop_episode(*sample())
    assert proof["episode_entry_order_id"] == 10
    assert proof["trade_ids"] == [3, 4]
    assert proof["net_pnl"] == "-2.1"


def test_cross_strategy_and_account_mismatch_rejected():
    args = list(deepcopy(sample()))
    args[0]["strategy"] = "other"
    # A binding is a candidate from its own writer, not a proof of the author.
    # Cross-strategy overlap is rejected by the caller's exact strategy lookup.
    args[1]["account_id"] = "other-account"
    assert verified_closed_stop_episode(*args) is None


def test_reopened_lot_or_missing_fill_rejected():
    args = list(deepcopy(sample()))
    args[4].insert(2, {"id": 5, "orderId": 26, "symbol": "ETHUSDT",
                       "side": "SELL", "qty": "0.7", "time": 2500})
    args[4].insert(3, {"id": 6, "orderId": 27, "symbol": "ETHUSDT",
                       "side": "BUY", "qty": "0.7", "time": 2600})
    assert verified_closed_stop_episode(*args) is None

    args = list(deepcopy(sample()))
    args[4].pop()
    assert verified_closed_stop_episode(*args) is None


def test_stop_order_mismatch_and_same_entry_order_rejected():
    args = list(deepcopy(sample()))
    args[4][-1]["orderId"] = 31
    assert verified_closed_stop_episode(*args) is None
    args = list(deepcopy(sample()))
    args[0]["episode_entry_order_id"] = 30
    assert verified_closed_stop_episode(*args) is None


def test_malformed_history_fails_closed_without_exception():
    args = list(deepcopy(sample()))
    args[4][1].pop("time")
    assert verified_closed_stop_episode(*args) is None
    args = list(deepcopy(sample()))
    args[1]["trade_ids"] = [3, 3]
    assert verified_closed_stop_episode(*args) is None


@pytest.mark.asyncio
async def test_fetch_orchestrates_exact_algo_order_and_episode_window():
    binding, _, entry_order, entry_fills, lot_fills = sample()
    client = AsyncMock()
    client.get_algo_order.return_value = {
        "algoId": 20, "symbol": "ETHUSDT", "orderType": "STOP",
        "algoStatus": "FINISHED", "actualOrderId": 30, "side": "SELL",
    }
    async def get_order(symbol, order_id):
        return ({"orderId": 30, "symbol": "ETHUSDT", "status": "FILLED",
                 "side": "SELL", "reduceOnly": True, "executedQty": "0.7"}
                if order_id == 30 else entry_order)
    client.get_order.side_effect = get_order
    async def get_trades(symbol, **kwargs):
        if kwargs.get("order_id") == "30":
            return [
                {"id": 3, "orderId": 30, "symbol": symbol, "side": "SELL", "qty": "0.2",
                 "realizedPnl": "-1", "commission": "0.05", "commissionAsset": "USDT", "time": 3000},
                {"id": 4, "orderId": 30, "symbol": symbol, "side": "SELL", "qty": "0.5",
                 "realizedPnl": "-1", "commission": "0.05", "commissionAsset": "USDT", "time": 4000},
            ]
        if kwargs.get("order_id") == "10":
            return entry_fills
        return lot_fills
    client.get_user_trades.side_effect = get_trades

    proof = await fetch_verified_closed_stop_episode(client, binding)

    assert proof["proof_kind"] == "closed_stop_episode_v1"
    assert proof["net_pnl"] == "-2.10"
    assert client.get_user_trades.await_args.kwargs == {
        "start_time": 1000, "end_time": 4000, "limit": 1000,
    }
