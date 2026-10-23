"""Task 2：无未来数据访问（as_of 强制）与覆盖度 fail-closed。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ai_tuner.replay.data_portal import DataPortal
from ai_tuner.replay.models import DataBlocked, FutureDataAccess

from .conftest import make_bars, write_snapshot

UTC = timezone.utc
AS_OF = datetime(2026, 10, 22, tzinfo=UTC)
WINDOW_START = AS_OF - timedelta(hours=4)

PRICES = [
    ("100", "101", "99", "100.5"),
    ("100.5", "102", "100", "101.5"),
    ("101.5", "103", "101", "102.5"),
    ("102.5", "103.5", "98", "99"),
]


def _portal(tmp_path, **kwargs):
    directory = str(tmp_path / "snapshot")
    write_snapshot(
        directory,
        "btc_eth_aggressive",
        AS_OF,
        WINDOW_START,
        {"BTCUSDT|1h": make_bars("BTCUSDT", "1h", WINDOW_START, PRICES)},
        ["BTCUSDT"],
        ["1h"],
        funding_by_symbol={"BTCUSDT": [(WINDOW_START + timedelta(hours=2), "0.0001")]},
        trade_signals=[
            {"signal_id": "s1", "signal_time": (WINDOW_START + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        ],
        **kwargs
    )
    return DataPortal(directory, "btc_eth_aggressive", AS_OF, WINDOW_START)


def test_bars_within_as_of_are_returned_in_order(tmp_path):
    portal = _portal(tmp_path)
    bars = portal.bars("BTCUSDT", "1h")
    assert len(bars) == 4
    assert [bar.close for bar in bars] == [
        Decimal(value) for value in ("100.5", "101.5", "102.5", "99")
    ]
    assert bars[0].close_time == WINDOW_START + timedelta(hours=1)


def test_query_beyond_portal_as_of_raises_future_data_access(tmp_path):
    portal = _portal(tmp_path)
    with pytest.raises(FutureDataAccess) as excinfo:
        portal.bars("BTCUSDT", "1h", as_of=AS_OF + timedelta(seconds=1))
    assert excinfo.value.interface == "bars"
    assert excinfo.value.offending_field == "as_of"


def test_bar_at_future_close_time_raises(tmp_path):
    portal = _portal(tmp_path)
    with pytest.raises(FutureDataAccess):
        portal.bar_at("BTCUSDT", "1h", AS_OF + timedelta(hours=1))


def test_missing_universe_is_blocked(tmp_path):
    portal = _portal(tmp_path, extra_files={"universe_slices.json": "[]"})
    with pytest.raises(DataBlocked) as excinfo:
        portal.universe()
    assert "universe_missing" in " ".join(excinfo.value.gaps)


def test_universe_is_selected_by_observed_at_not_current_value(tmp_path):
    portal = _portal(tmp_path)
    universe = portal.universe()
    assert universe.symbols == ("BTCUSDT",)
    assert universe.source == "strategy_config"


def test_funding_rates_are_windowed_and_ordered(tmp_path):
    portal = _portal(tmp_path)
    rates = portal.funding_rates("BTCUSDT", WINDOW_START, AS_OF)
    assert rates == ((WINDOW_START + timedelta(hours=2), Decimal("0.0001")),)


def test_trade_signals_and_config_version(tmp_path):
    portal = _portal(tmp_path)
    signals = portal.trade_signals()
    assert len(signals) == 1
    hashes = portal.config_version()
    assert hashes.base_config_hash == "b" * 64
    assert hashes.overlay_hash == "d" * 64


def test_config_hash_missing_is_blocked(tmp_path):
    portal = _portal(
        tmp_path,
        extra_files={"config_hashes.json": '{"base_config_hash":"b","params_manifest_hash":"","overlay_hash":"d"}'},
    )
    with pytest.raises(DataBlocked) as excinfo:
        portal.config_version()
    assert "config_hash_missing" in " ".join(excinfo.value.gaps)


def test_coverage_report_reports_kline_ratio_and_gaps(tmp_path):
    portal = _portal(tmp_path)
    report = portal.coverage_report()
    assert report["kline_coverage"] == "100.0000"
    assert report["missing_symbol_intervals"] == []
    assert report["funding_gaps"] == []
    # 合成夹具无完整外部快照 → 覆盖率 0 且判 BLOCKED_DATA（fail-closed）
    assert report["external_snapshot_coverage"] == "0.0000"
    assert report["data_complete"] is False


def test_kline_shortfall_is_reported_as_gap(tmp_path):
    directory = str(tmp_path / "snapshot")
    write_snapshot(
        directory,
        "btc_eth_aggressive",
        AS_OF,
        WINDOW_START,
        {"BTCUSDT|1h": make_bars("BTCUSDT", "1h", WINDOW_START, PRICES[:2])},
        ["BTCUSDT"],
        ["1h"],
        funding_by_symbol={"BTCUSDT": [(WINDOW_START + timedelta(hours=2), "0.0001")]},
    )
    report = DataPortal(directory, "btc_eth_aggressive", AS_OF, WINDOW_START).coverage_report()
    assert report["missing_symbol_intervals"] == [
        {"symbol": "BTCUSDT", "interval": "1h", "expected": 4, "actual": 2}
    ]
    assert report["data_complete"] is False
