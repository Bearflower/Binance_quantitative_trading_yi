"""隔离的 XAU 校准版影子 profile 契约测试。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from strategies.btc_eth.shadow.collector import ShadowCollector, _build_shadow_strategy
from strategies.btc_eth.strategy import BTCEthStrategy as OriginalBTCEthStrategy


_CONFIG_DIR = (
    Path(__file__).resolve().parents[2]
    / "strategies"
    / "btc_eth"
    / "shadow"
    / "config"
)


def _load_profile(profile_id: str) -> dict:
    path = _CONFIG_DIR / f"{profile_id}.yaml"
    assert path.is_file(), f"profile 配置不存在: {path}"
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _remove_path(config: dict, path: tuple[str, ...]) -> None:
    cursor = config
    for key in path[:-1]:
        next_cursor = cursor.get(key)
        if not isinstance(next_cursor, dict):
            return
        cursor = next_cursor
    cursor.pop(path[-1], None)


def _closed_klines(count: int, step_ms: int, end_ms: int) -> list[dict]:
    rows = []
    for index in range(count):
        close_time = end_ms - (count - 1 - index) * step_ms
        price = 3900.0 + index
        rows.append({
            "open_time": close_time - step_ms + 1,
            "close_time": close_time,
            "open": price,
            "high": price + 12.0,
            "low": price - 8.0,
            "close": price + 3.0,
            "volume": 1000.0,
            "trades": 50,
        })
    return rows


def _closed_multi_timeframe_klines() -> dict[str, list[dict]]:
    scheduled = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
    end_ms = int(scheduled.timestamp() * 1000) - 1
    return {
        "1h": _closed_klines(120, 3_600_000, end_ms),
        "4h": _closed_klines(100, 14_400_000, end_ms),
        "1d": _closed_klines(60, 86_400_000, end_ms),
    }


def _collector_for_single_profile(profile_id: str, config: dict, strategy) -> ShadowCollector:
    collector = ShadowCollector(
        run_id="xau-calibrated-shadow-test",
        instance_id=config["instance"]["id"],
        symbol="XAUUSDT",
        profiles=[profile_id],
        profile_configs={profile_id: config},
        repository=MagicMock(),
        kline_service=MagicMock(),
        binance_client=MagicMock(),
    )
    collector._strategies[profile_id] = strategy
    return collector


def _close_times(multi: dict[str, list[dict]]) -> dict[str, int]:
    return {interval: rows[-1]["close_time"] for interval, rows in multi.items()}


def test_calibrated_profile_only_differs_from_original_on_allowlist():
    original = deepcopy(_load_profile("xau_original"))
    calibrated = deepcopy(_load_profile("xau_calibrated"))

    allowed_paths = (
        ("instance", "id"),
        ("instance", "profiles"),
        ("shadow", "cron"),
        ("shadow", "run_id_prefix"),
        ("shadow", "bootstrap_dynamic_atr_history"),
        ("shadow", "calibration_manifest"),
        ("shadow", "calibration_manifest_sha256"),
        ("shadow", "calibration_snapshot_sha256"),
        ("shadow", "calibration_cutoff_ms"),
        ("strategy", "name"),
        ("strategy", "version"),
        ("strategy", "record_name"),
        ("strategy", "symbol_config", "XAUUSDT", "atr_abs_min"),
        ("strategy", "symbol_config", "XAUUSDT", "atr_fallback_percent"),
        ("strategy", "risk", "dynamic_atr", "min_samples"),
        ("strategy", "risk", "dynamic_atr", "min_history_count"),
    )
    for path in allowed_paths:
        _remove_path(original, path)
        _remove_path(calibrated, path)

    assert calibrated == original


def test_calibrated_profile_has_frozen_identity_schedule_and_dynamic_atr():
    config = _load_profile("xau_calibrated")

    assert config["instance"] == {
        **config["instance"],
        "id": "mtcps_xau_calibrated_shadow",
        "initial_virtual_equity_usdt": 300,
        "profiles": ["xau_calibrated"],
    }
    assert config["shadow"]["kline_intervals"] == ["1h", "4h", "1d"]
    assert config["shadow"]["cron"] == "20 * * * *"
    assert config["shadow"]["run_id_prefix"] == "xau-calibrated-shadow"
    assert config["shadow"]["bootstrap_dynamic_atr_history"] is True
    assert config["shadow"]["calibration_manifest"] == (
        "strategies/btc_eth/shadow/config/calibration/"
        "xau_calibrated_atr_manifest.json"
    )

    strategy = config["strategy"]
    assert strategy["name"] == "mtcps_xau_calibrated"
    assert strategy["version"] == "2.6.0-xau-shadow-calibrated"
    assert strategy["record_name"] == "mtcps_xau_shadow_calibrated"
    assert strategy["symbol_config"]["XAUUSDT"]["atr_abs_min"] == 0.00153
    assert strategy["symbol_config"]["XAUUSDT"]["atr_fallback_percent"] == 0.317

    dynamic_atr = strategy["risk"]["dynamic_atr"]
    assert dynamic_atr == {
        "enabled": True,
        "lookback_hours": 720,
        "min_history_count": 30,
        "cache_ttl_seconds": 3600,
    }
    assert "min_samples" not in dynamic_atr


def test_calibrated_profile_uses_original_strategy_and_fallback_override():
    config = _load_profile("xau_calibrated")

    strategy = _build_shadow_strategy(
        profile_id="xau_calibrated",
        profile_config=config,
        binance_client=MagicMock(),
        kline_service=MagicMock(),
    )

    assert type(strategy) is OriginalBTCEthStrategy
    assert strategy.execution_mode == "shadow_batch"
    assert strategy.real_ordering_enabled is False
    assert strategy.state_reporting_enabled is False
    assert strategy.atr_filter.symbol_overrides["XAUUSDT"] == {
        "absolute_min_atr_percent": 0.153,
        "percentile": 0.5,
        "strong_coefficient": 0.8,
        "fallback_atr_percent": 0.317,
    }


@pytest.mark.asyncio
async def test_calibrated_profile_rebuilds_atr_history_each_decision():
    config = _load_profile("xau_calibrated")
    strategy = _build_shadow_strategy(
        profile_id="xau_calibrated",
        profile_config=config,
        binance_client=MagicMock(),
        kline_service=MagicMock(),
    )
    strategy._analyze_stationary_signal = AsyncMock(return_value=(None, "评分不足"))
    collector = _collector_for_single_profile("xau_calibrated", config, strategy)
    multi = _closed_multi_timeframe_klines()
    scheduled = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)

    first = await collector._analyze_profile(
        profile_id="xau_calibrated",
        strategy=strategy,
        multi_klines=multi,
        close_times=_close_times(multi),
        scheduled_at=scheduled,
    )
    first_history = list(strategy.atr_filter._atr_history["XAUUSDT"])
    strategy.atr_filter._atr_history["XAUUSDT"].clear()
    second = await collector._analyze_profile(
        profile_id="xau_calibrated",
        strategy=strategy,
        multi_klines=multi,
        close_times=_close_times(multi),
        scheduled_at=scheduled,
    )
    second_history = list(strategy.atr_filter._atr_history["XAUUSDT"])

    assert first_history == second_history
    assert len(first_history) >= 30
    assert first[0]["decision"] == "no_signal"
    assert first[0]["feature_snapshot"]["atr_filter"]["history_count"] >= 30
    assert (
        first[0]["feature_snapshot"]["atr_filter"]
        == second[0]["feature_snapshot"]["atr_filter"]
    )


@pytest.mark.asyncio
async def test_original_profile_does_not_use_deterministic_bootstrap(monkeypatch):
    config = _load_profile("xau_original")
    strategy = _build_shadow_strategy(
        profile_id="xau_original",
        profile_config=config,
        binance_client=MagicMock(),
        kline_service=MagicMock(),
    )
    strategy._analyze_stationary_signal = AsyncMock(return_value=(None, "评分不足"))
    initialize = MagicMock(wraps=strategy.atr_filter.initialize_history)
    monkeypatch.setattr(strategy.atr_filter, "initialize_history", initialize)
    collector = _collector_for_single_profile("xau_original", config, strategy)
    multi = _closed_multi_timeframe_klines()

    await collector._analyze_profile(
        profile_id="xau_original",
        strategy=strategy,
        multi_klines=multi,
        close_times=_close_times(multi),
        scheduled_at=datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc),
    )

    initialize.assert_not_called()


@pytest.mark.asyncio
async def test_calibrated_bootstrap_error_is_fail_closed():
    config = _load_profile("xau_calibrated")
    strategy = _build_shadow_strategy(
        profile_id="xau_calibrated",
        profile_config=config,
        binance_client=MagicMock(),
        kline_service=MagicMock(),
    )
    strategy.atr_filter.enabled = False
    strategy._analyze_stationary_signal = AsyncMock(return_value=(None, "不应执行"))
    collector = _collector_for_single_profile("xau_calibrated", config, strategy)
    multi = _closed_multi_timeframe_klines()

    events = await collector._analyze_profile(
        profile_id="xau_calibrated",
        strategy=strategy,
        multi_klines=multi,
        close_times=_close_times(multi),
        scheduled_at=datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc),
    )

    assert events[0]["decision"] == "analysis_error"
    assert events[0]["reason_codes"] == ["atr_bootstrap_error"]
    strategy._analyze_stationary_signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_candidate_keeps_features_and_adds_atr_filter_snapshot():
    config = _load_profile("xau_calibrated")
    strategy = _build_shadow_strategy(
        profile_id="xau_calibrated",
        profile_config=config,
        binance_client=MagicMock(),
        kline_service=MagicMock(),
    )
    strategy._analyze_stationary_signal = AsyncMock(return_value=(
        {
            "direction": "LONG",
            "grade": "A",
            "score": 80.0,
            "entry_price": 4020.0,
            "initial_stop_loss": 3990.0,
            "tp1_price": 4050.0,
            "tp2_price": 4080.0,
            "atr": 20.0,
            "market_state": "RANGING",
            "price_change_1h": 0.012,
        },
        "",
    ))
    collector = _collector_for_single_profile("xau_calibrated", config, strategy)
    multi = _closed_multi_timeframe_klines()

    events = await collector._analyze_profile(
        profile_id="xau_calibrated",
        strategy=strategy,
        multi_klines=multi,
        close_times=_close_times(multi),
        scheduled_at=datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc),
    )

    snapshot = events[0]["feature_snapshot"]
    assert snapshot["market_state"] == "RANGING"
    assert snapshot["atr"] == 20.0
    assert snapshot["price_change_1h"] == 0.012
    assert snapshot["atr_filter"]["history_count"] >= 30
    assert snapshot["atr_filter"]["final_threshold"] is not None
