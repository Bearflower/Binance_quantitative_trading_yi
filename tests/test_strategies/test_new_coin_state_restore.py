import json
from unittest.mock import AsyncMock

import pytest

from strategies.new_coin import strategy as strategy_module


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", [False, True])
async def test_restore_state_reads_state_data_instead_of_row(monkeypatch, encoded):
    state = {
        "positions": {},
        "traded_symbols": [],
        "consecutive_losses": 1,
        "cumulative_pnl": "-1.72949999",
        "state_version": 2,
        "peak_pnl": "0",
    }
    strategy = strategy_module.NewCoinStrategy.__new__(strategy_module.NewCoinStrategy)
    strategy.db = AsyncMock()
    strategy.db.fetch_one.return_value = {"state_data": json.dumps(state) if encoded else state}
    strategy.strategy_name = "new_coin"
    strategy._rebuild_position_baseline = AsyncMock()
    monkeypatch.setattr(strategy_module, "get_open_orders", AsyncMock(return_value=[]))

    await strategy._restore_state()

    assert strategy.consecutive_losses == 1
    assert str(strategy.cumulative_pnl) == "-1.72949999"
    strategy._rebuild_position_baseline.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_state_rejects_malformed_persisted_state(monkeypatch):
    strategy = strategy_module.NewCoinStrategy.__new__(strategy_module.NewCoinStrategy)
    strategy.db = AsyncMock()
    strategy.db.fetch_one.return_value = {"state_data": "not-json"}
    strategy.strategy_name = "new_coin"
    strategy._rebuild_position_baseline = AsyncMock()
    monkeypatch.setattr(strategy_module, "get_open_orders", AsyncMock(return_value=[]))

    with pytest.raises(ValueError):
        await strategy._restore_state()
    strategy._rebuild_position_baseline.assert_not_awaited()
