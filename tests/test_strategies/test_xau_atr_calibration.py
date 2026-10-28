import copy
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from shared.indicators import TechnicalIndicators
from strategies.btc_eth.shadow.atr_calibration import (
    CalibrationError,
    build_calibration,
    canonical_snapshot_bytes,
    prepare_runtime_atr_history,
    verify_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
CALIBRATION_SCRIPT = ROOT / "scripts" / "generate_xau_atr_calibration.py"
CALIBRATED_PROFILE = (
    ROOT / "strategies" / "btc_eth" / "shadow" / "config" / "xau_calibrated.yaml"
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
    expected_mask = expected_atr.notna() & (expected_atr > 0)
    expected_atr_values = expected_atr.loc[expected_mask].astype(float).tolist()
    expected_closes = (
        included.iloc[:-1].loc[expected_mask, "close"].astype(float).tolist()
    )
    assert len(atr_values) == len(closes) == 46
    assert atr_values == pytest.approx(expected_atr_values)
    assert closes == pytest.approx(expected_closes)


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


def test_invalid_interval_and_non_monotonic_open_time_are_excluded_deterministically():
    rows = _rows()
    invalid_close_time = rows[20]["close_time"] - 1_800_000
    non_monotonic_open = dict(
        rows[20],
        open_time=rows[0]["open_time"],
        close_time=invalid_close_time,
    )
    short_close_time = rows[40]["close_time"] + 1_800_000
    short_interval = dict(
        rows[40],
        open_time=short_close_time - 1_799_999,
        close_time=short_close_time,
    )

    forward = build_calibration(
        [*rows, non_monotonic_open, short_interval],
        cutoff_ms=rows[-1]["close_time"],
    )
    reverse = build_calibration(
        [short_interval, non_monotonic_open, *reversed(rows)],
        cutoff_ms=rows[-1]["close_time"],
    )

    assert forward.exclusions == reverse.exclusions == {"invalid_interval": 2}
    assert forward.snapshot_rows == reverse.snapshot_rows
    assert all(
        current["open_time"] > previous["open_time"]
        for previous, current in zip(
            forward.snapshot_rows, forward.snapshot_rows[1:]
        )
    )
    assert all(
        row["close_time"] - row["open_time"] == 3_599_999
        for row in forward.snapshot_rows
    )


def test_result_evidence_is_not_shared_with_input_or_manifest():
    rows = _rows()
    result = build_calibration(rows, cutoff_ms=rows[-1]["close_time"])
    first_snapshot_open = result.snapshot_rows[0]["open"]
    rows[0]["open"] = -1

    manifest = result.to_manifest(
        source="kline-service-postgres",
        git_commit="abc123",
        snapshot_sha256=result.snapshot_sha256,
        generated_at="2026-10-27T08:00:00+00:00",
    )
    manifest["exclusions"]["mutated"] = 1
    manifest["gaps"].append(
        {"previous_close_time": 1, "next_close_time": 2, "delta_ms": 1}
    )
    manifest["calibration"]["p50_percent"] = -1

    assert result.snapshot_rows[0]["open"] == first_snapshot_open
    assert "mutated" not in result.exclusions
    assert result.gaps == []
    assert result.p50_percent > 0


def _run_calibration_cli(
    tmp_path,
    source_rows,
    *,
    json_array=False,
    cutoff_ms=None,
    git_commit="abc123",
    extra_args=(),
):
    source = tmp_path / ("source.json" if json_array else "source.jsonl")
    if json_array:
        source.write_text(json.dumps(source_rows), encoding="utf-8")
    else:
        source.write_text(
            "".join(json.dumps(row) + "\n" for row in source_rows),
            encoding="utf-8",
        )
    snapshot = tmp_path / "snapshot.jsonl"
    manifest = tmp_path / "manifest.json"
    profile = tmp_path / "xau_calibrated.yaml"
    shutil.copyfile(CALIBRATED_PROFILE, profile)
    command = [
        sys.executable,
        str(CALIBRATION_SCRIPT),
        "--input",
        str(source),
        "--snapshot-output",
        str(snapshot),
        "--manifest-output",
        str(manifest),
        "--profile-config",
        str(profile),
        "--cutoff-ms",
        str(source_rows[-1]["close_time"] if cutoff_ms is None else cutoff_ms),
        "--source",
        "test-fixture",
        "--git-commit",
        git_commit,
        *extra_args,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed, snapshot, manifest, profile, command


def test_cli_generates_verified_evidence_and_atomically_updates_profile(tmp_path):
    rows = _rows()
    completed, snapshot, manifest_path, profile_path, _command = (
        _run_calibration_cli(tmp_path, rows)
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_manifest(manifest, snapshot.read_bytes())
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    symbol = profile["strategy"]["symbol_config"]["XAUUSDT"]
    shadow = profile["shadow"]
    assert symbol["atr_abs_min"] == manifest["calibration"]["atr_abs_min"]
    assert symbol["atr_fallback_percent"] == manifest["calibration"][
        "fallback_atr_percent"
    ]
    assert shadow["calibration_manifest_sha256"] == manifest["manifest_sha256"]
    assert shadow["calibration_snapshot_sha256"] == manifest["snapshot_sha256"]
    assert shadow["calibration_cutoff_ms"] == manifest["cutoff_ms"]
    assert shadow["calibration_manifest"].endswith(
        "calibration/xau_calibrated_atr_manifest.json"
    )

    summary = json.loads(completed.stdout)
    assert summary["generated_at"] == manifest["generated_at"]
    assert datetime.fromisoformat(summary["generated_at"]).tzinfo is not None
    assert summary["snapshot_sha256"] == manifest["snapshot_sha256"]
    assert summary["manifest_sha256"] == manifest["manifest_sha256"]
    assert summary["atr_abs_min"] == manifest["calibration"]["atr_abs_min"]
    assert summary["fallback_atr_percent"] == manifest["calibration"][
        "fallback_atr_percent"
    ]
    assert datetime.fromisoformat(manifest["generated_at"]).tzinfo is not None
    assert completed.stdout.count("\n") == 1


def test_cli_accepts_json_array_max_cutoff_and_auto_git_commit(tmp_path):
    rows = _rows()
    completed, snapshot, manifest_path, _profile, command = _run_calibration_cli(
        tmp_path,
        rows,
        json_array=True,
        cutoff_ms="max",
        git_commit="auto",
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_manifest(manifest, snapshot.read_bytes())
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert manifest["cutoff_ms"] == max(row["close_time"] for row in rows)
    assert manifest["git_commit"] == expected_sha


def test_cli_refuses_different_existing_outputs_without_replace(tmp_path):
    rows = _rows()
    completed, snapshot, manifest, profile, command = _run_calibration_cli(
        tmp_path, rows
    )
    assert completed.returncode == 0, completed.stderr
    snapshot.write_bytes(b"do-not-overwrite\n")
    before_manifest = manifest.read_bytes()
    before_profile = profile.read_bytes()

    refused = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert refused.returncode != 0
    assert snapshot.read_bytes() == b"do-not-overwrite\n"
    assert manifest.read_bytes() == before_manifest
    assert profile.read_bytes() == before_profile


@pytest.mark.parametrize("invalid_payload", ["not-json\n", "[]\n"])
def test_cli_invalid_or_empty_input_leaves_profile_unchanged(tmp_path, invalid_payload):
    source = tmp_path / "invalid.json"
    source.write_text(invalid_payload, encoding="utf-8")
    snapshot = tmp_path / "snapshot.jsonl"
    manifest = tmp_path / "manifest.json"
    profile = tmp_path / "xau_calibrated.yaml"
    shutil.copyfile(CALIBRATED_PROFILE, profile)
    before_profile = profile.read_bytes()

    completed = subprocess.run(
        [
            sys.executable,
            str(CALIBRATION_SCRIPT),
            "--input",
            str(source),
            "--snapshot-output",
            str(snapshot),
            "--manifest-output",
            str(manifest),
            "--profile-config",
            str(profile),
            "--cutoff-ms",
            "max",
            "--source",
            "test-fixture",
            "--git-commit",
            "abc123",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert profile.read_bytes() == before_profile
    assert not snapshot.exists()
    assert not manifest.exists()


def test_cli_profile_validation_failure_writes_no_evidence(tmp_path):
    rows = _rows()
    source = tmp_path / "source.jsonl"
    source.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    snapshot = tmp_path / "snapshot.jsonl"
    manifest = tmp_path / "manifest.json"
    profile = tmp_path / "invalid-profile.yaml"
    profile.write_text("strategy: {}\nshadow: {}\n", encoding="utf-8")
    before_profile = profile.read_bytes()

    completed = subprocess.run(
        [
            sys.executable,
            str(CALIBRATION_SCRIPT),
            "--input",
            str(source),
            "--snapshot-output",
            str(snapshot),
            "--manifest-output",
            str(manifest),
            "--profile-config",
            str(profile),
            "--cutoff-ms",
            str(rows[-1]["close_time"]),
            "--source",
            "test-fixture",
            "--git-commit",
            "abc123",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert profile.read_bytes() == before_profile
    assert not snapshot.exists()
    assert not manifest.exists()
