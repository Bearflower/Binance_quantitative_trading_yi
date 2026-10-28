"""隔离的 XAU 校准版影子 profile 契约测试。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from strategies.btc_eth.shadow.collector import _build_shadow_strategy
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
