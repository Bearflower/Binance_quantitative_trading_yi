"""Entry intent records the executed route without changing decisions or prices."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
import yaml

from strategies.btc_eth.shadow.collector import ShadowCollector
from strategies.btc_eth.strategy import BTCEthStrategy as OriginalStrategy
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggressiveStrategy


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_cls", [OriginalStrategy, AggressiveStrategy])
@pytest.mark.parametrize(
    "market_state,behavior,expected_intent,expected_fallback",
    [
        ("STRONG_TREND", {"strategy_mode": "ranging"}, "mean_reversion", False),
        ("RANGING", {"strategy_mode": "trend"}, "trend", False),
        ("RANGING", {}, "trend", True),
        ("RANGING", {"strategy_mode": None}, "trend", False),
        ("RANGING", {"strategy_mode": "unexpected"}, "trend", False),
    ],
)
async def test_stationary_intent_follows_executed_route_without_numeric_changes(
    strategy_cls, market_state, behavior, expected_intent, expected_fallback
):
    # Avoid exchange/database construction; execute the real stationary method.
    strategy = strategy_cls.__new__(strategy_cls)
    strategy.symbol_config = {}
    strategy.atr_filter = None
    strategy.scoring_config = {"min_score": 50}
    strategy.risk_config = {"ranging_strategy": {"risk": {"stop_loss_atr": 2}}}
    strategy._check_ranging_entry = MagicMock(return_value=(True, "LONG"))
    strategy._check_trend_filter = MagicMock(return_value=(True, "LONG"))
    strategy._check_prohibited_conditions = AsyncMock(return_value=(True, ""))
    strategy._calculate_score = MagicMock(return_value=88.5)
    strategy._determine_grade = MagicMock(return_value="A")
    strategy._apply_overheat_downgrade = MagicMock(return_value="A")
    strategy._check_volume_filter = MagicMock(return_value=(True, ""))
    strategy._get_grade_risk = MagicMock(return_value={"stop_loss_atr_multiplier": 3})
    strategy._get_optimized_price = AsyncMock(return_value=None)
    strategy._calculate_tp_price = MagicMock(
        side_effect=[Decimal("4020"), Decimal("4040")]
    )
    klines = {"1h": [{"close": 3900}, {"close": 4000}]}
    indicators = {"1h": {"ATR": pd.Series([10])}}
    original_behavior = deepcopy(behavior)
    original_klines = deepcopy(klines)
    original_atr = indicators["1h"]["ATR"].copy()

    signal, reason = await strategy._analyze_stationary_signal(
        "XAUUSDT", klines, indicators, market_state, behavior
    )

    assert reason == ""
    assert signal is not None
    is_ranging = expected_intent == "mean_reversion"
    assert strategy._check_ranging_entry.call_count == int(is_ranging)
    assert strategy._check_trend_filter.call_count == int(not is_ranging)
    assert signal["entry_intent"] == expected_intent
    assert signal["entry_route_fallback"] is expected_fallback
    assert {
        key: value for key, value in signal.items()
        if key not in {"entry_intent", "entry_route_fallback"}
    } == {
        "direction": "LONG", "grade": "A", "score": 88.5,
        "entry_price": Decimal("4000"),
        "initial_stop_loss": Decimal("3980" if is_ranging else "3970"),
        "atr": Decimal("10"), "market_state": market_state,
        "price_change_1h": 100 / 3900,
        "tp1_price": Decimal("4020"), "tp2_price": Decimal("4040"),
    }
    assert behavior == original_behavior
    assert klines == original_klines
    pd.testing.assert_series_equal(indicators["1h"]["ATR"], original_atr)


@pytest.fixture(params=["xau_original", "xau_aggressive", "xau_calibrated"])
def collector_and_profile(request):
    profile = request.param
    config_path = (Path(__file__).resolve().parents[2]
                   / "strategies/btc_eth/shadow/config" / f"{profile}.yaml")
    config = yaml.safe_load(config_path.read_text())
    collector = ShadowCollector(
        run_id="entry-intent-test", instance_id=config["instance"]["id"],
        symbol="XAUUSDT", profiles=[profile], profile_configs={profile: config},
        repository=MagicMock(), kline_service=MagicMock(), binance_client=MagicMock(),
    )
    expected_class = AggressiveStrategy if profile == "xau_aggressive" else OriginalStrategy
    assert isinstance(collector._strategies[profile], expected_class)
    return collector, profile


@pytest.mark.parametrize(
    "intent_fields,expected_intent,expected_fallback",
    [
        ({"entry_intent": "trend", "entry_route_fallback": False}, "trend", False),
        ({"entry_intent": "mean_reversion", "entry_route_fallback": False}, "mean_reversion", False),
        ({"entry_intent": "trend", "entry_route_fallback": True}, "trend", True),
        ({}, "unknown", None),
        ({"entry_intent": None}, "unknown", None),
        ({"entry_intent": "ranging"}, "unknown", None),
        ({"entry_intent": ""}, "unknown", None),
        ({"entry_intent": ["trend"]}, "unknown", None),
        ({"entry_intent": {"value": "trend"}}, "unknown", None),
    ],
)
def test_collector_preserves_intent_metadata_and_input(
    collector_and_profile, intent_fields, expected_intent, expected_fallback
):
    collector, profile = collector_and_profile
    signal = {
        "direction": "LONG", "grade": "A", "score": 88.5,
        "entry_price": Decimal("4000"), "initial_stop_loss": Decimal("3980"),
        "tp1_price": Decimal("4020"), "tp2_price": Decimal("4040"),
        "atr": Decimal("10"), "price_change_1h": 0.01,
        # Legacy RANGING must never be silently inferred to mean_reversion.
        "market_state": "RANGING", **intent_fields,
    }
    original_signal = deepcopy(signal)
    now = datetime(2026, 11, 25, tzinfo=timezone.utc)
    event = collector._make_event_from_signal(
        profile_id=profile, scheduled_at=now, evaluated_at=now,
        signal_components=signal, close_times={"1h": 1234},
        atr_filter_snapshot={"history_count": 40},
    )

    snapshot = event["feature_snapshot"]
    assert snapshot["entry_intent"] == expected_intent
    assert snapshot["entry_route_fallback"] is expected_fallback
    assert snapshot["market_state"] == "RANGING"
    assert snapshot["atr"] == 10.0
    assert snapshot["price_change_1h"] == 0.01
    assert snapshot["atr_filter"] == {"history_count": 40}
    assert event["reference_price"] == event["intended_entry_price"] == 4000.0
    assert event["direction"] == "LONG"
    assert event["grade"] == "A"
    assert event["score"] == 88.5
    assert event["decision"] == "candidate"
    assert signal == original_signal

@pytest.mark.parametrize('decision,quality,status', [
    ('no_signal', None, 'no_candidate'),
    ('no_signal', 'missing_klines', 'data_unavailable'),
    ('analysis_error', None, 'analysis_error'),
])
def test_every_non_candidate_persists_explicit_attribution_state(collector_and_profile, decision, quality, status):
    collector, profile = collector_and_profile
    snapshot = {'atr_filter': {'history_count': 86}}
    before = deepcopy(snapshot)
    now = datetime(2026, 11, 26, tzinfo=timezone.utc)
    event = collector._make_event(profile_id=profile, scheduled_at=now, evaluated_at=now,
        decision=decision, data_quality_reason=quality, feature_snapshot=snapshot)
    assert event['feature_snapshot']['entry_attribution'] == {
        'schema_version': 1, 'status': status, 'intent': None,
        'producer_git_commit': collector.git_commit,
    }
    assert snapshot == before
    assert event['feature_snapshot']['atr_filter'] == before['atr_filter']
    assert event['decision'] == decision
    assert event['intended_order_type'] is None

@pytest.mark.parametrize('intent,status', [('trend','recorded'), ('mean_reversion','recorded'), (None,'unknown')])
def test_candidate_attribution_state_matches_actual_signal(collector_and_profile, intent, status):
    collector, profile = collector_and_profile
    now = datetime(2026, 11, 26, tzinfo=timezone.utc)
    event = collector._make_event_from_signal(profile_id=profile, scheduled_at=now,
        evaluated_at=now, signal_components={'entry_intent':intent,'entry_price':4000},close_times={})
    assert event['feature_snapshot']['entry_attribution'] == {
        'schema_version':1, 'status':status, 'intent':intent if intent else 'unknown',
        'producer_git_commit':collector.git_commit,
    }
