from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from shared.protection_retry import place_and_wait_entry_order
from strategies.btc_eth.strategy import BTCEthStrategy as Original
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as Aggressive


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_class", [Original, Aggressive])
@pytest.mark.parametrize(
    "behavior,intent",
    [({}, "trend"), ({"strategy_mode": "ranging"}, "mean_reversion")],
)
async def test_entry_intent_comes_from_actual_branch(strategy_class, behavior, intent):
    strategy = strategy_class.__new__(strategy_class)
    strategy.symbol_config = {}
    strategy.risk_config = {}
    strategy.scoring_config = {"min_score": 0}
    strategy.atr_filter = None
    strategy._check_ranging_entry = lambda *args: (True, "LONG")
    strategy._check_trend_filter = lambda *args: (True, "LONG")
    strategy._check_prohibited_conditions = AsyncMock(return_value=(True, ""))
    strategy._calculate_score = lambda *args: 80
    strategy._determine_grade = lambda *args: "A"
    strategy._apply_overheat_downgrade = lambda grade, *args: grade
    strategy._check_volume_filter = lambda *args: (True, "")
    strategy._get_grade_risk = lambda *args: {"stop_loss_atr_multiplier": 2}
    strategy._get_optimized_price = AsyncMock(return_value=None)
    strategy._calculate_tp_price = lambda *args: Decimal("105")
    bars = {"1h": [{"close": "99"}, {"close": "100"}]}
    indicators = {"1h": {"ATR": pd.Series([2])}}
    signal, reason = await strategy._analyze_stationary_signal(
        "BTCUSDT", bars, indicators, "RANGING", behavior
    )
    assert reason == ""
    assert signal["market_state"] == "RANGING"
    assert signal["entry_intent"] == intent


@pytest.mark.asyncio
async def test_original_acknowledged_order_is_linked_even_without_fill():
    strategy = SimpleNamespace(
        my_record_name="btc_eth",
        binance=SimpleNamespace(place_order=AsyncMock(return_value={
            "orderId": 123, "clientOrderId": "test_entry", "status": "NEW",
        })),
        risk_config={},
        _wait_for_order_fill=AsyncMock(return_value=None),
    )
    signal = {"direction": "LONG", "quantity": 1, "entry_price": 100}
    assert await place_and_wait_entry_order(strategy, "BTCUSDT", signal) is None
    assert signal["entry_order_id"] == 123
    assert signal["entry_client_order_id"] == "test_entry"


def test_retracement_has_symmetric_directions_and_no_same_bar_order():
    from backtest.btc_eth.replay.entry_research import retracement

    first = {"high": 106, "low": 100}
    second = {"high": 120, "low": 111}
    assert retracement([first, second], "LONG", 112.36)["fibonacci"]
    assert retracement([second, first], "SHORT", 107.64)["fibonacci"]
    assert retracement([first, second], "LONG", 114)["ordinary"]
    assert retracement([first, second], "LONG", 121) is None
    assert retracement([first, second], "SHORT", 110) is None
    assert retracement([{"high": 120, "low": 100}, second], "LONG", 112) is None


def test_reservation_geometry_preserves_intent_for_recovery():
    from shared.entry_risk_policy import entry_geometry

    signal = dict(direction="LONG", initial_stop_loss="98", tp1_price="104",
                  tp2_price="106", leverage=3, grade="A")
    filters = dict(stepSize="0.001", tickSize="0.01", minQty="0.001", minNotional="5")
    partial = dict(tp1_close_ratio="0.3", tp2_close_ratio="0.3")
    assert "entry_intent" not in entry_geometry(signal, filters, {}, partial)["signal"]
    signal["entry_intent"] = "mean_reversion"
    assert entry_geometry(signal, filters, {}, partial)["signal"]["entry_intent"] == "mean_reversion"


def test_decision_evidence_uses_actual_intent_on_fallback():
    from strategies.btc_eth.evidence_recorder import _build_entry_mode

    signal = {"market_state": "RANGING", "entry_intent": "trend", "entry_route_fallback": True}
    assert _build_entry_mode(signal, True) == "trend"


def test_replay_snapshot_uses_closed_candles_and_rejects_a_gap():
    from datetime import datetime, timedelta
    from backtest.btc_eth.replay.entry_research import FRAMES, snapshot

    at = datetime(2026, 11, 1)
    series = {}
    for frame, duration in FRAMES.items():
        bars = []
        for offset in range(60, -2, -1):
            opened = at - offset * duration
            bars.append({"open_time": opened, "close_time": opened + duration - timedelta(milliseconds=1)})
        series["BTCUSDT", frame] = ([bar["close_time"] for bar in bars], bars)
    result = snapshot(series, "BTCUSDT", at)
    assert all(bars[-1]["close_time"] < at for bars in result.values())

    closes, bars = series["BTCUSDT", "1h"]
    del closes[41]
    del bars[41]
    assert snapshot(series, "BTCUSDT", at) is None


@pytest.mark.asyncio
async def test_original_carries_intent_in_exchange_id_before_acknowledgement():
    send = AsyncMock(side_effect=TimeoutError)
    strategy = SimpleNamespace(
        my_record_name="btc_eth", binance=SimpleNamespace(place_order=send), risk_config={},
    )
    signal = {"direction": "LONG", "quantity": 1, "entry_price": 100, "entry_intent": "trend"}
    with pytest.raises(TimeoutError):
        await place_and_wait_entry_order(strategy, "BTCUSDT", signal)
    client_id = send.call_args.kwargs["client_order_id"]
    assert client_id.startswith("mt1t")
    assert len(client_id) == 36
    assert signal["entry_client_order_id"] == client_id
