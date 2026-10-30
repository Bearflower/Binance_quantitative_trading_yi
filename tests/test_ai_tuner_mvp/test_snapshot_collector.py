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
    # 批量接口返回 {symbol: provenance} 映射
    market_cap.get_batch_market_caps_with_provenance = AsyncMock(
        return_value={
            "BTCUSDT": SimpleNamespace(
                value=100_000.0, source="coingecko", as_of=AS_OF.timestamp(),
                available_at=AS_OF.timestamp(), is_proxy=False, missing_reason=None,
            ),
            "ETHUSDT": SimpleNamespace(
                value=200_000.0, source="coingecko", as_of=AS_OF.timestamp(),
                available_at=AS_OF.timestamp(), is_proxy=False, missing_reason=None,
            ),
        }
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
    # 批量接口只接收成功 symbol（BTCUSDT）的 oi_map
    market_cap.get_batch_market_caps_with_provenance = AsyncMock(
        return_value={
            "BTCUSDT": SimpleNamespace(
                value=100_000.0, source="coingecko", as_of=AS_OF.timestamp(),
                available_at=AS_OF.timestamp(), is_proxy=False, missing_reason=None,
            ),
        }
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
    market_cap.get_batch_market_caps_with_provenance = AsyncMock(return_value={
        "BTCUSDT": SimpleNamespace(
            value=100_000.0, source="coingecko", as_of=AS_OF.timestamp(),
            available_at=AS_OF.timestamp(), is_proxy=False, missing_reason=None,
        ),
    })

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


# ===========================================================================
# 批量市值相关新增用例（AC-1 / AC-4 / AC-5 / AC-6）
# ===========================================================================


def _mk_prov(value, source, is_proxy=False, missing_reason=None):
    """快速构造 provenance SimpleNamespace"""
    return SimpleNamespace(
        value=value, source=source, as_of=AS_OF.timestamp(),
        available_at=AS_OF.timestamp(), is_proxy=is_proxy,
        missing_reason=missing_reason,
    )


def _setup_binance_mocks():
    """为 symbol 集合准备 binance mock（无状态，不依赖传入 symbols）"""
    binance = MagicMock()
    binance.get_open_interest = AsyncMock(return_value=10.0)
    binance.get_funding_rate = AsyncMock(return_value=0.0001)
    binance.get_ticker = AsyncMock(return_value={"lastPrice": "100", "quoteVolume": "5000"})
    return binance


@pytest.mark.asyncio
async def test_batch_market_cap_called_once_per_bucket():
    """AC-1：单桶批量市值只调用 1 次，且传参为阶段一成功的 symbol 集合"""
    repository = MagicMock()
    repository.latest_universe = AsyncMock(return_value={"BTCUSDT", "ETHUSDT", "DOGEUSDT"})
    repository.store_market_snapshot = AsyncMock()
    binance = _setup_binance_mocks()
    market_cap = MagicMock()
    market_cap.get_batch_market_caps_with_provenance = AsyncMock(return_value={
        "BTCUSDT": _mk_prov(1e9, "coingecko"),
        "ETHUSDT": _mk_prov(3e8, "coingecko"),
        "DOGEUSDT": _mk_prov(1e7, "coingecko"),
    })
    collector = SnapshotCollector(repository, binance, market_cap, concurrency=3)

    await collector.collect_once(AS_OF)

    # 只调用 1 次
    assert market_cap.get_batch_market_caps_with_provenance.await_count == 1
    # 传参为完整 universe
    args = market_cap.get_batch_market_caps_with_provenance.await_args.args
    assert set(args[0]) == {"BTCUSDT", "ETHUSDT", "DOGEUSDT"}
    # oi_map 正确传入
    oi_map = args[1]
    # oi_usd = oi * price = 10 * 100 = 1000
    for sym in ["BTCUSDT", "ETHUSDT", "DOGEUSDT"]:
        assert oi_map[sym] == 1000.0


@pytest.mark.asyncio
async def test_source_mapping_complete_proxy_missing():
    """AC-4/AC-5：不同 source → 不同 summary 计数与 is_complete 口径"""
    repository = MagicMock()
    repository.latest_universe = AsyncMock(
        return_value={"BTCUSDT", "ETHUSDT", "DOGEUSDT", "NOPEUSDT"}
    )
    repository.store_market_snapshot = AsyncMock()
    binance = _setup_binance_mocks()
    market_cap = MagicMock()
    market_cap.get_batch_market_caps_with_provenance = AsyncMock(return_value={
        "BTCUSDT": _mk_prov(1e9, "coingecko"),
        "ETHUSDT": _mk_prov(3e8, "cache_hit"),
        "DOGEUSDT": _mk_prov(20_000.0, "oi_proxy", is_proxy=True),   # oi_usd*20
        "NOPEUSDT": _mk_prov(0.0, "missing", missing_reason="no data"),
    })
    collector = SnapshotCollector(repository, binance, market_cap, concurrency=4)

    summary = await collector.collect_once(AS_OF)

    assert summary.requested == 4
    assert summary.complete == 2    # BTCUSDT(coingecko) + ETHUSDT(cache_hit)
    assert summary.proxy == 1      # DOGEUSDT(oi_proxy)
    assert summary.missing == 1    # NOPEUSDT(missing)
    assert summary.failed == 0


@pytest.mark.asyncio
async def test_batch_returns_empty_map_then_each_symbol_degrades():
    """AC-6：批量返回部分 oi_proxy/missing 时，collector 仍对全部 symbol 入库、failed==0

    注：真正的整批异常兜底由 shared 层 get_batch_market_caps_with_provenance 内部完成
    （另在 test_market_cap_provenance.py 覆盖）；这里 mock 出已降级的 provenance map，
    验证 collector 不因 provenance 来源而跳过入库、不抬高 failed 计数。
    """
    repository = MagicMock()
    repository.latest_universe = AsyncMock(
        return_value={"BTCUSDT", "ETHUSDT", "NOPEUSDT"}
    )
    repository.store_market_snapshot = AsyncMock()
    binance = _setup_binance_mocks()
    market_cap = MagicMock()
    # 全部返回缺失/代理（模拟批量层整体失败后 shared 层降级结果）
    market_cap.get_batch_market_caps_with_provenance = AsyncMock(return_value={
        "BTCUSDT": _mk_prov(20_000.0, "oi_proxy", is_proxy=True),   # oi_usd*20
        "ETHUSDT": _mk_prov(20_000.0, "oi_proxy", is_proxy=True),
        "NOPEUSDT": _mk_prov(0.0, "missing", missing_reason="no data"),
    })
    collector = SnapshotCollector(repository, binance, market_cap, concurrency=3)

    summary = await collector.collect_once(AS_OF)

    assert summary.requested == 3
    assert summary.complete == 0
    assert summary.proxy == 2
    assert summary.missing == 1
    assert summary.failed == 0      # 批量异常不抬高 failed 计数
    # 全部入库
    assert repository.store_market_snapshot.await_count == 3


@pytest.mark.asyncio
async def test_store_failure_midway_does_not_drop_remaining_symbols():
    """阶段三单 symbol 入库异常：其余 symbol 仍入库，failed 计 1、complete 正确

    回归深度审查阻断项：_persist_phase 曾缺 per-symbol 隔离，
    任一 symbol 入库抛错会中断整桶后续 symbol。
    """
    repository = MagicMock()
    repository.latest_universe = AsyncMock(
        return_value={"AUSDT", "BUSDT", "CUSDT"}
    )
    stored = []

    async def store(snapshot):
        if snapshot.symbol == "BUSDT":
            raise RuntimeError("db write failed")
        stored.append(snapshot.symbol)

    repository.store_market_snapshot = AsyncMock(side_effect=store)
    binance = _setup_binance_mocks()
    market_cap = MagicMock()
    market_cap.get_batch_market_caps_with_provenance = AsyncMock(return_value={
        "AUSDT": _mk_prov(1e9, "coingecko"),
        "BUSDT": _mk_prov(1e9, "coingecko"),
        "CUSDT": _mk_prov(1e9, "coingecko"),
    })
    collector = SnapshotCollector(repository, binance, market_cap, concurrency=3)

    summary = await collector.collect_once(AS_OF)

    # BUSDT 失败但不影响 AUSDT / CUSDT 入库
    assert set(stored) == {"AUSDT", "CUSDT"}
    assert summary.requested == 3
    assert summary.complete == 2
    assert summary.failed == 1
