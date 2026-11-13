from unittest.mock import AsyncMock
import pytest
from strategies.btc_eth.shadow.repository import ShadowRepository


@pytest.mark.asyncio
async def test_conflict_does_not_count_as_insert():
    db = AsyncMock()
    db.execute.side_effect = ["INSERT 0 1", "INSERT 0 0"]
    repo = ShadowRepository(db)
    assert await repo.insert_decision_event({}) is True
    assert await repo.insert_decision_event({}) is False
