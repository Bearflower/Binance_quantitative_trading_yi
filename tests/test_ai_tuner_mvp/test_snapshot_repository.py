from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ai_tuner.snapshots.models import MarketSnapshot
from ai_tuner.snapshots.repository import SnapshotRepository


AS_OF = datetime(2026, 10, 21, 10, 20, tzinfo=timezone.utc)


class FakeDatabase:
    def __init__(self):
        self.rows = {}

    async def execute(self, query, *args):
        if "ai_tuner_market_snapshots" not in query:
            raise AssertionError(query)
        key = (args[0], args[1], args[3])
        incoming = args
        current = self.rows.get(key)
        if current is None or args[-1] >= current[-1]:
            self.rows[key] = incoming
        return "INSERT 0 1"

    async def fetch_one(self, query, *args):
        if "COUNT(*)" not in query:
            raise AssertionError(query)
        return {"count": int(tuple(args) in self.rows)}


@pytest.fixture
def complete_snapshot():
    return MarketSnapshot(
        strategy_id="hrs",
        symbol="BTCUSDT",
        observed_at=AS_OF,
        bucket_start=AS_OF.replace(minute=0, second=0, microsecond=0),
        open_interest_contracts=10.0,
        open_interest_usd=1_000_000.0,
        funding_rate=0.0001,
        market_cap_usd=100_000_000.0,
        oi_market_cap_ratio=0.01,
        sentiment_score=10.95,
        sentiment_formula_version="funding_annual_pct_v1",
        oi_source="binance",
        funding_source="binance",
        market_cap_source="coingecko",
        source_observed_at=AS_OF,
        payload_hash="a" * 64,
        raw_payload={"source": "test"},
    )


@pytest.mark.asyncio
async def test_upsert_same_bucket_is_idempotent(complete_snapshot):
    db = FakeDatabase()
    repository = SnapshotRepository(db)
    await repository.store_market_snapshot(complete_snapshot)
    await repository.store_market_snapshot(complete_snapshot)
    assert await repository.count_bucket(complete_snapshot.key) == 1


def test_future_source_timestamp_is_rejected(complete_snapshot):
    with pytest.raises(ValueError, match="future source"):
        replace(
            complete_snapshot,
            source_observed_at=complete_snapshot.bucket_end + timedelta(seconds=1),
        )


def test_proxy_market_cap_is_saved_but_not_complete(complete_snapshot):
    snapshot = replace(complete_snapshot, market_cap_source="oi_proxy")
    assert snapshot.is_complete is False


@pytest.mark.asyncio
async def test_complete_snapshot_cannot_be_downgraded(complete_snapshot):
    db = FakeDatabase()
    repository = SnapshotRepository(db)
    await repository.store_market_snapshot(complete_snapshot)
    await repository.store_market_snapshot(
        replace(
            complete_snapshot,
            market_cap_usd=None,
            oi_market_cap_ratio=None,
            market_cap_source="missing",
            payload_hash="b" * 64,
        )
    )
    assert db.rows[complete_snapshot.key][6] == 100_000_000.0
