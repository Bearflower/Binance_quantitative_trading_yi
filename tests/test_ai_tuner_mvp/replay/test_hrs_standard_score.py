from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_tuner.replay.adapters.hrs import HrsAdapter
from ai_tuner.replay.models import Bar, DataBlocked, ExternalSnapshot, FutureDataAccess
from strategies.hrs.pattern import PatternRecognizer
from strategies.hrs.scoring_engine import ScoringEngine

AS_OF = datetime(2026, 11, 18, tzinfo=timezone.utc)
PRICE = Decimal("100")


def bars(interval, hours):
    return tuple(
        Bar("BTCUSDT", interval, AS_OF - timedelta(hours=hours * index),
            AS_OF - timedelta(hours=hours * (index - 1)),
            PRICE, PRICE, PRICE, PRICE, PRICE, PRICE, 1)
        for index in range(30, 0, -1)
    )


@pytest.fixture
def snapshot():
    return ExternalSnapshot(
        "hrs", "BTCUSDT", AS_OF - timedelta(minutes=1), AS_OF - timedelta(hours=1),
        PRICE, Decimal("0.001"), Decimal("1000"), Decimal("0.1"), Decimal("100"),
        "binance", "binance", "coingecko", "a" * 64, True,
        collected_at=AS_OF - timedelta(minutes=1),
    )


@pytest.mark.parametrize("direction", ["long", "short"])
def test_offline_standard_score_reuses_live_pattern_and_scoring(direction, snapshot):
    hourly, trend = bars("1h", 1), bars("4h", 4)
    score, entry = HrsAdapter.score_frozen_standard(
        {}, as_of=AS_OF, direction=direction, snapshot=snapshot, bars_1h=hourly, bars_4h=trend,
    )
    data = [{key: "100" for key in ("open", "high", "low", "close", "volume")}] * 30
    recognizer = PatternRecognizer({})
    patterns = (recognizer.detect_long_patterns(data) if direction == "long"
                else recognizer.detect_short_patterns(data))
    engine = ScoringEngine({})
    expected = engine.score("BTCUSDT", direction, .1, patterns, .001, klines_4h=data)
    assert score == expected
    assert entry == engine.should_entry(expected)


def test_offline_score_blocks_unavailable_snapshot_or_proxy(snapshot):
    kwargs = dict(as_of=AS_OF, direction="long", bars_1h=bars("1h", 1), bars_4h=bars("4h", 4))
    with pytest.raises(FutureDataAccess):
        HrsAdapter.score_frozen_standard({}, snapshot=replace(snapshot, collected_at=AS_OF + timedelta(seconds=1)), **kwargs)
    with pytest.raises(DataBlocked):
        HrsAdapter.score_frozen_standard({}, snapshot=replace(snapshot, market_cap_source="oi_proxy"), **kwargs)


def test_offline_score_rejects_missing_hour(snapshot):
    hourly = bars("1h", 1)
    with pytest.raises(DataBlocked, match="hrs_standard_score_discontinuous_bars"):
        HrsAdapter.score_frozen_standard(
            {}, as_of=AS_OF, direction="long", snapshot=snapshot,
            bars_1h=hourly[:12] + hourly[13:], bars_4h=bars("4h", 4),
        )


def frozen_scan(symbol_count=1):
    return dict(
        strategy_id="hrs", observed_at=AS_OF - timedelta(hours=1),
        created_at=AS_OF - timedelta(minutes=59), source_run_id="scan-1",
        config_hash="b" * 64, symbol_count=symbol_count,
    )


def frozen_member(scan, **overrides):
    return {**scan, "symbol": "BTCUSDT", "membership": "both", **overrides}


def test_frozen_candidate_scores_only_recorded_directions(snapshot):
    scan = frozen_scan()
    scores = HrsAdapter.score_frozen_candidate(
        {}, as_of=AS_OF, snapshot=snapshot, scan=scan,
        memberships=[frozen_member(scan, membership="short")],
        bars_1h=bars("1h", 1), bars_4h=bars("4h", 4),
    )
    assert tuple(scores) == ("short",)
    assert scores["short"][0] == HrsAdapter.score_frozen_standard(
        {}, as_of=AS_OF, direction="short", snapshot=snapshot,
        bars_1h=bars("1h", 1), bars_4h=bars("4h", 4),
    )[0]


def test_frozen_empty_scan_is_distinct_from_incomplete_scan(snapshot):
    kwargs = dict(as_of=AS_OF, snapshot=snapshot, memberships=[], bars_1h=(), bars_4h=())
    assert HrsAdapter.score_frozen_candidate({}, scan=frozen_scan(0), **kwargs) == {}
    with pytest.raises(DataBlocked, match="hrs_candidate_scan_incomplete"):
        HrsAdapter.score_frozen_candidate({}, scan=frozen_scan(), **kwargs)


@pytest.mark.parametrize("override", [
    {"source_run_id": "other"}, {"config_hash": "c" * 64},
    {"membership": "unknown"}, {"observed_at": AS_OF},
])
def test_frozen_candidate_blocks_mixed_scan_identity(snapshot, override):
    scan = frozen_scan()
    with pytest.raises(DataBlocked):
        HrsAdapter.score_frozen_candidate(
            {}, as_of=AS_OF, snapshot=snapshot, scan=scan,
            memberships=[frozen_member(scan, **override)],
            bars_1h=(), bars_4h=(),
        )


def test_frozen_candidate_blocks_late_publication(snapshot):
    scan = frozen_scan()
    with pytest.raises(FutureDataAccess):
        HrsAdapter.score_frozen_candidate(
            {}, as_of=AS_OF, snapshot=snapshot, scan=scan,
            memberships=[frozen_member(scan, created_at=AS_OF + timedelta(seconds=1))],
            bars_1h=(), bars_4h=(),
        )


def test_dynamic_candidate_blocks_uncaptured_history(snapshot):
    scan = frozen_scan()
    config = {"candidate_pool": {"dynamic_thresholds": {"enabled": True}}}
    kwargs = dict(as_of=AS_OF, snapshot=snapshot, memberships=[frozen_member(scan)],
                  bars_1h=bars("1h", 1), bars_4h=bars("4h", 4))
    with pytest.raises(DataBlocked, match="hrs_dynamic_thresholds_history_missing"):
        HrsAdapter.score_frozen_candidate(config, scan=scan, **kwargs)
    fixed = {"kind": "frozen_dynamic_thresholds_v1", "mode": "fixed"}
    assert set(HrsAdapter.score_frozen_candidate(config, scan={**scan, "dynamic_thresholds": fixed},
                                                  **kwargs)) == {"long", "short"}


def test_dynamic_candidate_rejects_threshold_computed_after_scan(snapshot):
    from strategies.hrs.candidate_pool import DynamicThresholds

    scan = frozen_scan()
    latest = DynamicThresholds()
    latest.sample_count = latest.funding_sample_count = latest.oi_market_cap_sample_count = 15
    record = {**latest.to_dict(), "kind": "frozen_dynamic_thresholds_v1", "mode": "dynamic",
              "computed_at": (scan["observed_at"] + timedelta(seconds=1)).isoformat()}
    config = {"candidate_pool": {"dynamic_thresholds": {"enabled": True}}}
    with pytest.raises(FutureDataAccess):
        HrsAdapter.score_frozen_candidate(
            config, as_of=AS_OF, snapshot=snapshot,
            scan={**scan, "dynamic_thresholds": record}, memberships=[frozen_member(scan)],
            bars_1h=bars("1h", 1), bars_4h=bars("4h", 4),
        )


def test_dynamic_candidate_restores_recorded_threshold_state(snapshot, monkeypatch):
    from strategies.hrs.candidate_pool import DynamicThresholds

    scan = frozen_scan()
    latest = DynamicThresholds()
    latest.sample_count = latest.funding_sample_count = latest.oi_market_cap_sample_count = 15
    latest.funding_rate_emm_short = 17.5
    scan["dynamic_thresholds"] = {
        **latest.to_dict(), "kind": "frozen_dynamic_thresholds_v1", "mode": "dynamic",
        "computed_at": scan["observed_at"].isoformat(),
    }
    received = []

    def score(*_args, dynamic_thresholds, **_kwargs):
        received.append(dynamic_thresholds)
        return None, False

    monkeypatch.setattr(HrsAdapter, "score_frozen_standard", staticmethod(score))
    config = {"candidate_pool": {"dynamic_thresholds": {"enabled": True}}}
    HrsAdapter.score_frozen_candidate(
        config, as_of=AS_OF, snapshot=snapshot, scan=scan,
        memberships=[frozen_member(scan, membership="short")],
        bars_1h=bars("1h", 1), bars_4h=bars("4h", 4),
    )
    assert len(received) == 1 and received[0].is_valid()
    assert received[0].funding_rate_emm_short == 17.5
