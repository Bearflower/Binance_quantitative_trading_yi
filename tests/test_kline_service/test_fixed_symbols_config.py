"""Production fixed-symbol defaults must not schedule the retired MATIC contract."""
from pathlib import Path
import runpy


def test_fixed_symbols_exclude_retired_matic_without_replacing_it():
    config_path = (Path(__file__).resolve().parents[2]
                   / "services/kline_service/shared/core/config.py")
    # This suite stubs shared.core.config; load the real defaults explicitly.
    settings_type = runpy.run_path(str(config_path))["Settings"]
    symbols = settings_type.model_construct().fixed_symbols_list
    assert "MATICUSDT" not in symbols
    # Removing an unavailable contract must not opt into a different contract.
    assert "POLUSDT" not in symbols
    assert {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"} <= set(symbols)
