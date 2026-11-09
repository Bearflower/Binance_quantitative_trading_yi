"""A binding is durable identity, not exit proof."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from shared.accounting.stop_binding import record_stop_binding


@pytest.mark.asyncio
async def test_replay_is_idempotent_when_persisted_identity_matches():
    db = AsyncMock()
    db.fetch_one.return_value = {
        "strategy": "btc_eth_aggressive", "episode_entry_order_id": 1001,
        "stop_side": "SELL", "stop_quantity": Decimal("0.3"),
    }
    kwargs = dict(account_id="default", strategy="btc_eth_aggressive", symbol="ETHUSDT",
                  episode_entry_order_id=1001, algo_id=2001, stop_side="SELL",
                  stop_quantity=Decimal("0.3"))
    assert await record_stop_binding(db, **kwargs)
    assert await record_stop_binding(db, **kwargs)
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_conflicting_episode_and_missing_identity_are_rejected():
    db = AsyncMock()
    db.fetch_one.return_value = {
        "strategy": "other", "episode_entry_order_id": 1001,
        "stop_side": "SELL", "stop_quantity": Decimal("0.3"),
    }
    kwargs = dict(account_id="default", strategy="btc_eth_aggressive", symbol="ETHUSDT",
                  episode_entry_order_id=1001, algo_id=2001, stop_side="SELL",
                  stop_quantity=Decimal("0.3"))
    assert not await record_stop_binding(db, **kwargs)
    assert not await record_stop_binding(db, **(kwargs | {"episode_entry_order_id": None}))
    assert db.execute.await_count == 1
