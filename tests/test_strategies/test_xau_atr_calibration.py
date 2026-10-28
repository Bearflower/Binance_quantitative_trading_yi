import copy
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from shared.indicators import TechnicalIndicators
from strategies.btc_eth.shadow.atr_calibration import (
    CalibrationError,
    build_calibration,
    canonical_snapshot_bytes,
    prepare_runtime_atr_history,
    verify_manifest,
)


def _rows(count=60, end_ms=1_793_059_199_999):
    rows = []
    for index in range(count):
        close_time = end_ms - (count - 1 - index) * 3_600_000
        base = 3900.0 + index
        rows.append(
            {
                "open_time": close_time - 3_599_999,
                "close_time": close_time,
                "open": base,
                "high": base + 12.0,
                "low": base - 8.0,
                "close": base + 3.0,
                "volume": 1000.0,
                "trades": 50,
            }
        )
    return rows


def test_calibration_reuses_production_atr_and_percentiles():
    rows = _rows()
    result = build_calibration(rows, cutoff_ms=rows[-1]["close_time"])

    frame = pd.DataFrame(rows)
    expected_atr = TechnicalIndicators.calculate_atr(frame, period=14).dropna()
    expected_close = frame.loc[expected_atr.index, "close"]
    expected_percent = expected_atr / expected_close * 100

    assert result.valid_rows == 60
    assert result.atr_sample_count == len(expected_percent)
    assert result.p25_percent == pytest.approx(
        np.percentile(expected_percent.to_numpy(), 25)
    )
    assert result.p50_percent == pytest.approx(
        np.percentile(expected_percent.to_numpy(), 50)
    )
    assert result.atr_abs_min == pytest.approx(result.p25_percent / 100.0)


def test_invalid_future_duplicate_and_zero_activity_rows_are_counted():
    rows = _rows()
    future = dict(rows[-1], close_time=rows[-1]["close_time"] + 3_600_000)
    duplicate = dict(rows[-1])
    zero_activity = dict(
        rows[0],
        open_time=rows[0]["open_time"] - 3_600_000,
        close_time=rows[0]["close_time"] - 3_600_000,
        volume=0,
        trades=0,
    )
    flat_price = rows[0]["close"]
    flat = dict(
        rows[0],
        open_time=rows[0]["open_time"] - 7_200_000,
        close_time=rows[0]["close_time"] - 7_200_000,
        open=flat_price,
        high=flat_price,
        low=flat_price,
        close=flat_price,
    )

    result = build_calibration(
        rows + [future, duplicate, zero_activity, flat],
        cutoff_ms=rows[-1]["close_time"],
    )

    assert result.exclusions == {
        "duplicate_close_time": 1,
        "future_or_open": 1,
        "non_positive_activity": 1,
        "zero_range": 1,
    }


def test_manifest_hash_tamper_fails_closed():
    rows = _rows()
    result = build_calibration(rows, cutoff_ms=rows[-1]["close_time"])
    snapshot_bytes = canonical_snapshot_bytes(result.snapshot_rows)
    manifest = result.to_manifest(
        source="kline-service-postgres",
        git_commit="abc123",
        snapshot_sha256=result.snapshot_sha256,
        generated_at="2026-10-27T08:00:00+00:00",
    )

    verify_manifest(manifest, snapshot_bytes)
    tampered = copy.deepcopy(manifest)
    tampered["calibration"]["p50_percent"] += 0.01
    with pytest.raises(CalibrationError, match="manifest_sha256"):
        verify_manifest(tampered, snapshot_bytes)


def test_snapshot_hash_tamper_fails_closed():
    rows = _rows()
    result = build_calibration(rows, cutoff_ms=rows[-1]["close_time"])
    manifest = result.to_manifest(
        source="kline-service-postgres",
        git_commit="abc123",
        snapshot_sha256=result.snapshot_sha256,
        generated_at="2026-10-27T08:00:00+00:00",
    )

    with pytest.raises(CalibrationError, match="snapshot_sha256"):
        verify_manifest(manifest, canonical_snapshot_bytes(result.snapshot_rows) + b"x")


def test_runtime_history_excludes_current_bar_and_future_bars():
    rows = _rows()
    future = dict(
        rows[-1],
        open_time=rows[-1]["open_time"] + 3_600_000,
        close_time=rows[-1]["close_time"] + 3_600_000,
    )
    rows.append(future)
    scheduled_at = datetime.fromtimestamp(
        _rows()[-1]["close_time"] / 1000,
        tz=timezone.utc,
    )
    atr_values, closes = prepare_runtime_atr_history(rows, scheduled_at=scheduled_at)

    included = pd.DataFrame(_rows())
    expected_atr = TechnicalIndicators.calculate_atr(included, period=14).iloc[:-1]
    expected_count = int((expected_atr.notna() & (expected_atr > 0)).sum())
    assert len(atr_values) == len(closes) == expected_count == 46
    assert all(value > 0 for value in atr_values)


def test_build_calibration_fails_closed_when_too_few_rows():
    rows = _rows(count=29)
    with pytest.raises(CalibrationError, match="valid_rows_below_30:29"):
        build_calibration(rows, cutoff_ms=rows[-1]["close_time"])


def test_trade_count_compatibility_gaps_and_non_finite_input():
    rows = _rows()
    missing = rows.pop(30)
    for row in rows:
        row["trade_count"] = row.pop("trades")
    invalid = dict(
        rows[0],
        open_time=rows[0]["open_time"] - 3_600_000,
        close_time=rows[0]["close_time"] - 3_600_000,
        high=float("nan"),
    )

    result = build_calibration(
        [invalid, *reversed(rows)], cutoff_ms=rows[-1]["close_time"]
    )

    assert result.exclusions == {"invalid_shape": 1}
    assert result.gaps == [
        {
            "previous_close_time": missing["close_time"] - 3_600_000,
            "next_close_time": missing["close_time"] + 3_600_000,
            "delta_ms": 7_200_000,
        }
    ]
    assert result.snapshot_rows[0]["trades"] == 50
