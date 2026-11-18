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
