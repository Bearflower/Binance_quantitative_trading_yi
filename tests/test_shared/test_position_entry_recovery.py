from unittest.mock import AsyncMock

import pytest

from shared.position_entry_recovery import fetch_lot_fills


@pytest.mark.asyncio
async def test_fetch_lot_fills_covers_every_time_window_without_gaps():
    client = AsyncMock()
    client.get_user_trades.side_effect = [[{"id": 1}], [{"id": 2}]]
    six_days = 6 * 24 * 60 * 60 * 1000

    rows = await fetch_lot_fills(client, "BTCUSDT", 1000, 1000 + six_days)

    assert rows == [{"id": 1}, {"id": 2}]
    assert client.get_user_trades.await_args_list[0].kwargs == {
        "start_time": 1000, "end_time": 1000 + six_days - 1, "limit": 1000,
    }
    assert client.get_user_trades.await_args_list[1].kwargs == {
        "start_time": 1000 + six_days, "end_time": 1000 + six_days, "limit": 1000,
    }


@pytest.mark.asyncio
async def test_fetch_lot_fills_rejects_a_truncated_window():
    client = AsyncMock()
    client.get_user_trades.return_value = [{"id": i} for i in range(1000)]

    assert await fetch_lot_fills(client, "BTCUSDT", 1000, 2000) is None
