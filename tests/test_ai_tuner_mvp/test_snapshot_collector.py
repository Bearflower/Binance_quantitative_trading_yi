from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_tuner.snapshots.collector import SnapshotCollector
from ai_tuner.snapshots.cli import build_parser, collect_safely, is_failed_bucket
from ai_tuner.snapshots.collector import CollectionSummary
from strategies.hrs.strategy import HRSStrategy


AS_OF = datetime(2026, 10, 21, 10, 20, tzinfo=timezone.utc)


def test_snapshot_cli_exposes_safe_commands():
    parser = build_parser()
    assert parser.parse_args(["once"]).command == "once"
    assert parser.parse_args(["daemon"]).command == "daemon"
    args = parser.parse_args([
        "backfill-supported", "--start", "2026-10-01T00:00:00Z",
        "--end", "2026-10-02T00:00:00Z",
    ])
    assert args.command == "backfill-supported"


def test_only_total_collection_failure_counts_as_failed_bucket():
    assert is_failed_bucket(CollectionSummary(2, 0, 0, 0, 2)) is True
    assert is_failed_bucket(CollectionSummary(2, 1, 0, 0, 1)) is False
    assert is_failed_bucket(CollectionSummary(2, 0, 2, 0, 0)) is True
    assert is_failed_bucket(CollectionSummary(2, 0, 0, 2, 0)) is True
    assert is_failed_bucket(CollectionSummary(0, 0, 0, 0, 0)) is False


@pytest.mark.asyncio
async def test_daemon_survives_top_level_collection_failure():
    collector = MagicMock()
    collector.collect_once = AsyncMock(side_effect=RuntimeError("db unavailable"))
    summary = await collect_safely(collector, AS_OF)
    assert summary.failed == 1
    assert is_failed_bucket(summary) is True


@pytest.mark.asyncio
async def test_collector_uses_only_observed_universe():
    repository = MagicMock()
    repository.latest_universe = AsyncMock(return_value={"BTCUSDT", "ETHUSDT"})
    repository.store_market_snapshot = AsyncMock()
    binance = MagicMock()
    binance.get_open_interest = AsyncMock(return_value=10.0)
    binance.get_funding_rate = AsyncMock(return_value=0.0001)
    binance.get_ticker = AsyncMock(return_value={"lastPrice": "100", "quoteVolume": "5000"})
    market_cap = MagicMock()
    market_cap.get_market_cap_with_provenance = AsyncMock(
        return_value=SimpleNamespace(
            value=100_000.0,
            source="coingecko",
            as_of=AS_OF.timestamp(),
            available_at=AS_OF.timestamp(),
            is_proxy=False,
            missing_reason=None,
        )
    )
    collector = SnapshotCollector(repository, binance, market_cap, concurrency=2)

    summary = await collector.collect_once(AS_OF)

    requested = {call.args[0] for call in binance.get_open_interest.await_args_list}
    assert requested == {"BTCUSDT", "ETHUSDT"}
    assert summary.requested == 2
    assert summary.complete == 2
    assert summary.failed == 0


@pytest.mark.asyncio
async def test_single_symbol_failure_does_not_cancel_other_symbols():
    repository = MagicMock()
    repository.latest_universe = AsyncMock(return_value={"BTCUSDT", "BADUSDT"})
    repository.store_market_snapshot = AsyncMock()
    binance = MagicMock()

    async def oi(symbol):
        if symbol == "BADUSDT":
            raise RuntimeError("upstream failed")
        return 10.0

    binance.get_open_interest = AsyncMock(side_effect=oi)
    binance.get_funding_rate = AsyncMock(return_value=0.0001)
    binance.get_ticker = AsyncMock(return_value={"lastPrice": "100", "quoteVolume": "5000"})
    market_cap = MagicMock()
    market_cap.get_market_cap_with_provenance = AsyncMock(
        return_value=SimpleNamespace(
            value=100_000.0, source="coingecko", as_of=AS_OF.timestamp(),
            available_at=AS_OF.timestamp(), is_proxy=False, missing_reason=None,
        )
    )

    summary = await SnapshotCollector(repository, binance, market_cap).collect_once(AS_OF)

    assert summary.requested == 2
    assert summary.complete == 1
    assert summary.failed == 1
    assert repository.store_market_snapshot.await_count == 1


@pytest.mark.asyncio
async def test_raw_evidence_includes_market_cap_provenance_and_derivations():
    repository = MagicMock()
    repository.latest_universe = AsyncMock(return_value={"BTCUSDT"})
    repository.store_market_snapshot = AsyncMock()
    binance = MagicMock()
    binance.get_open_interest = AsyncMock(return_value=10.0)
    binance.get_funding_rate = AsyncMock(return_value=0.0001)
    binance.get_ticker = AsyncMock(return_value={"lastPrice": "100", "quoteVolume": "5000"})
    market_cap = MagicMock()
    market_cap.get_market_cap_with_provenance = AsyncMock(return_value=SimpleNamespace(
        value=100_000.0, source="coingecko", as_of=AS_OF.timestamp(),
        available_at=AS_OF.timestamp(), is_proxy=False, missing_reason=None,
    ))

    await SnapshotCollector(repository, binance, market_cap).collect_once(AS_OF)
    payload = repository.store_market_snapshot.await_args.args[0].raw_payload
    assert payload["market_cap_usd"] == 100_000.0
    assert payload["market_cap_as_of"] == AS_OF.timestamp()
    assert payload["oi_market_cap_ratio"] == 0.01
    assert payload["sentiment_formula_version"] == "funding_annual_pct_v1"


@pytest.mark.asyncio
async def test_universe_snapshot_failure_is_side_effect_only():
    strategy = HRSStrategy.__new__(HRSStrategy)
    strategy.snapshot_repository = MagicMock()
    strategy.snapshot_repository.store_universe = AsyncMock(side_effect=RuntimeError("db down"))
    strategy.candidate_pool = MagicMock()
    strategy.candidate_pool.get_short_candidates.return_value = {"BTCUSDT"}
    strategy.candidate_pool.get_long_candidates.return_value = {"ETHUSDT"}
    strategy.config = {"strategy": {"name": "hrs"}}

    await strategy._persist_universe_snapshot(AS_OF)

    strategy.snapshot_repository.store_universe.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_supported_never_invents_historical_market_cap():
    repository = MagicMock()
    repository.latest_universe = AsyncMock(return_value={"BTCUSDT"})
    repository.store_market_snapshot = AsyncMock()
    binance = MagicMock()
    binance.get_open_interest_hist = AsyncMock(return_value=[{
        "sumOpenInterest": "10", "sumOpenInterestValue": "1000",
        "timestamp": int(AS_OF.timestamp() * 1000),
    }])
    binance.get_funding_rate_history = AsyncMock(return_value=[{
        "fundingRate": "0.0001", "fundingTime": int(AS_OF.timestamp() * 1000),
    }])

    await SnapshotCollector(repository, binance, MagicMock()).backfill_supported(
        AS_OF.replace(minute=0), AS_OF.replace(minute=0).replace(hour=11)
    )

    snapshot = repository.store_market_snapshot.await_args.args[0]
    assert snapshot.market_cap_usd is None
    assert snapshot.market_cap_source == "missing"
    assert snapshot.is_complete is False
