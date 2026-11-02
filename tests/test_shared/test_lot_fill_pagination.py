from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from shared.position_entry_recovery import fetch_lot_fills, verified_anchored_lot


@pytest.mark.asyncio
async def test_fetches_complete_lot_across_exchange_seven_day_windows():
    anchor = 1_800_000_000_000
    client = AsyncMock()
    client.get_user_trades.side_effect = [
        [{"id": 1, "time": anchor, "side": "BUY", "qty": "3"}],
        [{"id": 2, "time": anchor + 7 * 86_400_000, "side": "SELL", "qty": "1"}],
    ]
    fills = await fetch_lot_fills(client, "ETHUSDT", anchor, anchor + 8 * 86_400_000)
    assert verified_anchored_lot(fills, Decimal("2"), anchor)
    assert client.get_user_trades.await_count == 2
    assert all(call.kwargs["end_time"] - call.kwargs["start_time"] < 7 * 86_400_000
               for call in client.get_user_trades.await_args_list)


@pytest.mark.asyncio
async def test_full_page_fails_closed_instead_of_treating_truncated_history_as_complete():
    anchor = 1_800_000_000_000
    client = AsyncMock()
    client.get_user_trades.return_value = [
        {"id": i, "time": anchor + i, "side": "BUY", "qty": "1"}
        for i in range(1000)
    ]
    assert await fetch_lot_fills(client, "ETHUSDT", anchor, anchor + 86_400_000) is None
