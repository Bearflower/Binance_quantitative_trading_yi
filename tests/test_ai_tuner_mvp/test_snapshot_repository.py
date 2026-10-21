from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from ai_tuner.snapshots.models import MarketSnapshot
from ai_tuner.snapshots.repository import SnapshotRepository


AS_OF = datetime(2026, 10, 21, 10, 20, tzinfo=timezone.utc)


def payload_hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class FakeDatabase:
    def __init__(self):
        self.rows = {}
        self.transactions = []
        self.fetch_all_result = []
        self.last_fetch_all_query = ""

    async def execute(self, query, *args):
        if "ai_tuner_market_snapshots" not in query:
            raise AssertionError(query)
        key = (args[0], args[1], args[3])
        incoming = args
        current = self.rows.get(key)
        if current is None or args[-1] > current[-1]:
            self.rows[key] = incoming
        return "INSERT 0 1"

    async def execute_transaction(self, queries):
        self.transactions.append(queries)
        return True

    async def fetch_one(self, query, *args):
        if "COUNT(*)" not in query:
            raise AssertionError(query)
        return {"count": int(tuple(args) in self.rows)}

    async def fetch_all(self, query, *args):
        self.last_fetch_all_query = query
        return self.fetch_all_result


@pytest.fixture
def complete_snapshot():
    raw_payload = {"source": "test"}
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
        payload_hash=payload_hash(raw_payload),
        raw_payload=raw_payload,
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


def test_derived_values_and_formula_are_validated(complete_snapshot):
    assert replace(complete_snapshot, oi_market_cap_ratio=0.02).is_complete is False
    assert replace(complete_snapshot, sentiment_score=999).is_complete is False
    assert replace(complete_snapshot, sentiment_formula_version="unknown").is_complete is False


def test_cache_hit_is_a_complete_real_source(complete_snapshot):
    assert replace(complete_snapshot, market_cap_source="cache_hit").is_complete is True


@pytest.mark.asyncio
async def test_complete_snapshot_cannot_be_downgraded(complete_snapshot):
    db = FakeDatabase()
    repository = SnapshotRepository(db)
    await repository.store_market_snapshot(complete_snapshot)
    missing_payload = {"source": "missing"}
    await repository.store_market_snapshot(
        replace(
            complete_snapshot,
            market_cap_usd=None,
            oi_market_cap_ratio=None,
            market_cap_source="missing",
            raw_payload=missing_payload,
            payload_hash=payload_hash(missing_payload),
        )
    )
    assert db.rows[complete_snapshot.key][6] == 100_000_000.0


@pytest.mark.asyncio
async def test_same_rank_snapshot_does_not_replace_existing_evidence(complete_snapshot):
    db = FakeDatabase()
    repository = SnapshotRepository(db)
    await repository.store_market_snapshot(complete_snapshot)
    alternate_payload = {"source": "alternate-direct"}
    await repository.store_market_snapshot(replace(
        complete_snapshot,
        raw_payload=alternate_payload,
        payload_hash=payload_hash(alternate_payload),
    ))
    assert db.rows[complete_snapshot.key][15] == complete_snapshot.payload_hash


@pytest.mark.asyncio
async def test_proxy_never_overwrites_incomplete_real_market_cap(complete_snapshot):
    db = FakeDatabase()
    repository = SnapshotRepository(db)
    real_incomplete_payload = {"source": "real-incomplete"}
    real_incomplete = replace(
        complete_snapshot,
        funding_rate=None,
        sentiment_score=None,
        raw_payload=real_incomplete_payload,
        payload_hash=payload_hash(real_incomplete_payload),
    )
    proxy_payload = {"source": "proxy"}
    proxy = replace(
        complete_snapshot,
        market_cap_source="oi_proxy",
        raw_payload=proxy_payload,
        payload_hash=payload_hash(proxy_payload),
    )
    await repository.store_market_snapshot(real_incomplete)
    await repository.store_market_snapshot(proxy)
    assert db.rows[complete_snapshot.key][13] == "coingecko"


@pytest.mark.asyncio
async def test_direct_source_upgrades_complete_cache_hit(complete_snapshot):
    db = FakeDatabase()
    repository = SnapshotRepository(db)
    cached_payload = {"source": "cache"}
    cached = replace(
        complete_snapshot,
        market_cap_source="cache_hit",
        raw_payload=cached_payload,
        payload_hash=payload_hash(cached_payload),
    )
    await repository.store_market_snapshot(cached)
    await repository.store_market_snapshot(complete_snapshot)
    assert db.rows[complete_snapshot.key][13] == "coingecko"


def test_payload_hash_must_match_canonical_payload(complete_snapshot):
    with pytest.raises(ValueError, match="does not match"):
        replace(complete_snapshot, payload_hash="a" * 64)


@pytest.mark.asyncio
async def test_universe_scan_is_atomic_and_empty_scan_is_representable():
    db = FakeDatabase()
    repository = SnapshotRepository(db)
    await repository.store_universe("hrs", AS_OF, [], [], "run-empty", "a" * 64)
    assert len(db.transactions) == 1
    queries = db.transactions[0]
    assert len(queries) == 1
    assert "ai_tuner_universe_scan_runs" in queries[0][0]
    assert queries[0][1][-1] == 0

    await repository.store_universe(
        "hrs", AS_OF + timedelta(hours=1), ["BTCUSDT"], ["ETHUSDT"],
        "run-two", "b" * 64,
    )
    assert len(db.transactions[1]) == 2
    member_query, member_args = db.transactions[1][1]
    assert "UNNEST" in member_query
    assert member_args[2] == ["BTCUSDT", "ETHUSDT"]
    assert member_args[3] == ["short", "long"]


@pytest.mark.asyncio
async def test_as_of_market_query_rejects_future_source_and_collection():
    db = FakeDatabase()
    await SnapshotRepository(db).latest_market_snapshots("hrs", AS_OF)
    assert "source_observed_at <= $2" in db.last_fetch_all_query
    assert "collected_at <= $2" in db.last_fetch_all_query
