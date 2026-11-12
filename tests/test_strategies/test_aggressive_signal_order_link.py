from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import json

import pytest

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy
from shared.mtpc_signal_persistence import persist_signal


@pytest.mark.asyncio
async def test_successful_new_entry_attaches_exact_exchange_order_to_signal():
    strategy = BTCEthStrategy.__new__(BTCEthStrategy)
    strategy.positions = {"BNBUSDT": SimpleNamespace(entry_order_id=1001,
                                                       episode_entry_order_id=1001)}
    strategy._persist_position_metadata = AsyncMock()
    signal = {"symbol": "BNBUSDT", "direction": "LONG"}

    with patch("strategies.btc_eth_aggressive.strategy.protection_retry.open_new_position",
               new_callable=AsyncMock, return_value=True):
        assert await strategy._open_new_position(signal) is True

    assert signal["entry_order_id"] == 1001
    strategy._persist_position_metadata.assert_awaited_once_with("BNBUSDT")


@pytest.mark.asyncio
async def test_rejected_entry_does_not_attach_stale_order_id():
    strategy = BTCEthStrategy.__new__(BTCEthStrategy)
    strategy.positions = {}
    strategy._persist_position_metadata = AsyncMock()
    signal = {"symbol": "BNBUSDT", "direction": "LONG"}

    with patch("strategies.btc_eth_aggressive.strategy.protection_retry.open_new_position",
               new_callable=AsyncMock, return_value=False):
        assert await strategy._open_new_position(signal) is False

    assert "entry_order_id" not in signal


@pytest.mark.asyncio
async def test_signal_persistence_keeps_order_identity_in_metadata():
    db = AsyncMock()
    signal = {"symbol": "BNBUSDT", "direction": "LONG", "grade": "B",
              "score": 66.25, "entry_price": 800, "trade_success": True,
              "entry_order_id": 1001}
    assert await persist_signal(db, "btc_eth_aggressive", signal) is True
    metadata = json.loads(db.execute.await_args.args[-1])
    assert metadata["entry_order_id"] == 1001
