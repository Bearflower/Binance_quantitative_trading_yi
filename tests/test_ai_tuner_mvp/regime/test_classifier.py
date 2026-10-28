"""Market-state grouping must use closed bars and explicit thresholds only."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_tuner.regime.classifier import classify_regime
from ai_tuner.regime.features import RegimeFeatures, extract_features
from ai_tuner.replay.models import Bar, DataBlocked, FutureDataAccess


UTC = timezone.utc
AS_OF = datetime(2026, 10, 28, tzinfo=UTC)


def _bar(index, close, *, high=None, low=None):
    close_time = AS_OF - timedelta(hours=8 - index)
    price = Decimal(str(close))
    return Bar(
        symbol="BTCUSDT", interval="1h", open_time=close_time - timedelta(hours=1),
        close_time=close_time, open=price,
        high=Decimal(str(high if high is not None else close)),
        low=Decimal(str(low if low is not None else close)), close=price,
        volume=Decimal("1"), quote_volume=price, trades=1,
    )


def test_future_bar_is_rejected_instead_of_used():
    bars = tuple(_bar(i, 100 + i) for i in range(8)) + (_bar(9, 200),)
    with pytest.raises(FutureDataAccess):
        extract_features(bars, AS_OF, min_segment_length=8)


def test_missing_threshold_blocks_classification():
    bars = tuple(_bar(i, 100 + i) for i in range(8))
    features = extract_features(bars, AS_OF, min_segment_length=8)
    with pytest.raises(DataBlocked):
        classify_regime(features, {"trend_return_pct": "2"})


@pytest.mark.parametrize(
    "signed_return,atr_pct,expected",
    [
        ("3", "1", "up_trend_normal"),
        ("3", "5", "up_trend_high_vol"),
        ("-3", "1", "down_trend_normal"),
        ("-3", "5", "down_trend_high_vol"),
        ("0", "1", "ranging_normal"),
        ("0", "5", "ranging_high_vol"),
        ("3", "10", "STRESS"),
    ],
)
def test_seven_market_states(signed_return, atr_pct, expected):
    features = RegimeFeatures(
        as_of=AS_OF,
        signed_return_pct=Decimal(signed_return),
        atr_pct=Decimal(atr_pct),
        bar_count=8,
    )
    assert classify_regime(features, {
        "trend_return_pct": "2",
        "high_volatility_atr_pct": "4.5",
        "stress_atr_pct": "9",
    }) == expected


def test_insufficient_closed_bars_blocks():
    with pytest.raises(DataBlocked):
        extract_features(tuple(_bar(i, 100) for i in range(7)), AS_OF, min_segment_length=8)


def test_mixed_symbol_bars_block_market_state():
    bars = list(_bar(i, 100 + i) for i in range(8))
    bars[-1] = Bar(**{**bars[-1].__dict__, "symbol": "ETHUSDT"})
    with pytest.raises(DataBlocked):
        extract_features(bars, AS_OF, min_segment_length=8)
