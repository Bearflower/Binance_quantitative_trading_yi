"""Local snapshot sealing is integrity evidence, never live provenance evidence."""

from __future__ import annotations

import json
import importlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_tuner.replay.data_portal import DataPortal
from ai_tuner.replay.models import DataBlocked

from .conftest import make_bars, write_snapshot


AS_OF = datetime(2026, 10, 22, tzinfo=timezone.utc)
START = AS_OF - timedelta(hours=4)


def _source(tmp_path: Path, strategy: str = "btc_eth_aggressive") -> Path:
    source = tmp_path / "source"
    bars = {"BTCUSDT|1h": make_bars("BTCUSDT", "1h", START, [("100", "101", "99", "100")])}
    write_snapshot(
        str(source), strategy, AS_OF, START, bars, ["BTCUSDT"], ["1h"],
    )
    return source


def _seal(tmp_path: Path, source: Path, **kwargs):
    return _sealer()(
        source, tmp_path / "output", "btc_eth_aggressive", AS_OF, START, AS_OF,
        **kwargs,
    )


def _sealer():
    return importlib.import_module("ai_tuner.replay.snapshot_sealer").seal_snapshot


def test_same_input_produces_identical_manifest_and_data_portal_accepts(tmp_path):
    source = _source(tmp_path)
    one = _seal(tmp_path / "one", source)
    two = _seal(tmp_path / "two", source)
    assert one["status"] == two["status"] == "SEALED"
    first = Path(one["snapshot_dir"])
    second = Path(two["snapshot_dir"])
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    manifest = json.loads((first / "manifest.json").read_text())
    assert manifest["source_provenance"] == "unverified"
    assert manifest["manifest_version"] == 2
    assert one["envelope_sha256"] == manifest["envelope_sha256"]
    assert one["source_attested"] is False
    assert DataPortal(first, "btc_eth_aggressive", AS_OF, START).manifest_sha256 == manifest["sha256"]
    assert DataPortal(
        first, "btc_eth_aggressive", AS_OF, START,
        expected_envelope_sha256=one["envelope_sha256"],
    ).manifest_sha256 == manifest["sha256"]


@pytest.mark.parametrize("field,value", [
    ("strategy_id", "hrs"),
    ("as_of", "2026-10-23T00:00:00Z"),
    ("window_start", "2026-10-21T00:00:00Z"),
    ("window_end", "2026-10-21T23:00:00Z"),
    ("source_provenance", "live"),
])
def test_v2_metadata_tamper_is_detected(tmp_path, field, value):
    result = _seal(tmp_path, _source(tmp_path))
    directory = Path(result["snapshot_dir"])
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(DataBlocked, match="manifest_envelope_hash_mismatch"):
        DataPortal(directory, "btc_eth_aggressive", AS_OF, START)


def test_v2_downgrade_is_detected_when_expected_digest_is_pinned(tmp_path):
    result = _seal(tmp_path, _source(tmp_path))
    directory = Path(result["snapshot_dir"])
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("manifest_version")
    manifest.pop("envelope_sha256")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(DataBlocked, match="manifest_v2_required"):
        DataPortal(
            directory, "btc_eth_aggressive", AS_OF, START,
            expected_envelope_sha256=result["envelope_sha256"],
        )


def test_modified_data_file_is_detected(tmp_path):
    result = _seal(tmp_path, _source(tmp_path))
    directory = Path(result["snapshot_dir"])
    with (directory / "funding_rates.jsonl").open("ab") as handle:
        handle.write(b"changed\n")
    with pytest.raises(DataBlocked, match="snapshot_tampered"):
        DataPortal(directory, "btc_eth_aggressive", AS_OF, START)


@pytest.mark.parametrize("missing", ["config_hashes.json", "universe_slices.json", "exchange_info.json", "funding_rates.jsonl", "trade_signals.jsonl", "klines/BTCUSDT_1h.jsonl"])
def test_missing_required_file_blocks_without_output(tmp_path, missing):
    source = _source(tmp_path)
    (source / missing).unlink()
    result = _seal(tmp_path, source)
    assert result["decision"] == "BLOCKED_DATA"
    expected = "klines/*.jsonl" if missing.startswith("klines/") else missing
    assert any(expected in gap for gap in result["gaps"])
    assert not (tmp_path / "output" / "snapshots").exists()


def test_hrs_requires_actual_market_snapshot_file(tmp_path):
    source = _source(tmp_path, "hrs")
    (source / "market_snapshots.jsonl").unlink()
    result = _sealer()(source, tmp_path / "output", "hrs", AS_OF, START, AS_OF)
    assert result["decision"] == "BLOCKED_DATA"
    assert "dataset_missing:market_snapshots.jsonl" in result["gaps"]


def test_each_declared_universe_kline_file_is_required(tmp_path):
    source = _source(tmp_path)
    universe = json.loads((source / "universe_slices.json").read_text())
    universe[0]["symbols"].append("ETHUSDT")
    (source / "universe_slices.json").write_text(json.dumps(universe))
    result = _seal(tmp_path, source)
    assert result["decision"] == "BLOCKED_DATA"
    assert "dataset_missing:klines/ETHUSDT_1h.jsonl" in result["gaps"]


@pytest.mark.parametrize("files", [["../outside.jsonl"], ["funding_rates.jsonl", "funding_rates.jsonl"]])
def test_escape_or_duplicate_file_list_is_rejected(tmp_path, files):
    result = _seal(tmp_path, _source(tmp_path), file_paths=files)
    assert result["decision"] == "BLOCKED_DATA"
    assert not (tmp_path / "output" / "snapshots").exists()


def test_symlink_is_rejected(tmp_path):
    source = _source(tmp_path)
    (source / "funding_rates.jsonl").unlink()
    (source / "funding_rates.jsonl").symlink_to(source / "trade_signals.jsonl")
    result = _seal(tmp_path, source)
    assert result["decision"] == "BLOCKED_DATA"
    assert any("symlink" in gap for gap in result["gaps"])


def test_output_root_symlink_is_rejected(tmp_path):
    source = _source(tmp_path)
    redirect = tmp_path / "redirect"
    redirect.mkdir()
    output = tmp_path / "output"
    output.symlink_to(redirect, target_is_directory=True)
    result = seal_snapshot_for_test(source, output)
    assert result["decision"] == "BLOCKED_DATA"
    assert not (redirect / "snapshots").exists()


def test_source_ancestor_symlink_is_rejected(tmp_path):
    real = tmp_path / "real"
    source = _source(real)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    result = _seal(tmp_path, linked / "source")
    assert result["decision"] == "BLOCKED_DATA"
    assert any("symlink" in gap for gap in result["gaps"])


def test_output_ancestor_symlink_is_rejected(tmp_path):
    source = _source(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    result = seal_snapshot_for_test(source, linked / "output")
    assert result["decision"] == "BLOCKED_DATA"
    assert not (real / "output").exists()


def test_repo_output_must_be_git_ignored(tmp_path):
    source = _source(tmp_path)
    repo = Path(__file__).resolve().parents[3]
    result = seal_snapshot_for_test(source, repo / "docs")
    assert result["decision"] == "BLOCKED_DATA"
    assert "output_not_git_ignored" in result["gaps"]


def seal_snapshot_for_test(source: Path, output: Path):
    return _sealer()(source, output, "btc_eth_aggressive", AS_OF, START, AS_OF)


def test_claimed_live_is_not_promoted(tmp_path):
    result = _seal(tmp_path, _source(tmp_path), claimed_source_provenance="live")
    assert result["decision"] == "BLOCKED_DATA"
    assert "source_provenance_unverifiable:live" in result["gaps"]


def test_no_overwrite_and_naive_timestamp_rejected(tmp_path):
    source = _source(tmp_path)
    result = _seal(tmp_path, source)
    assert result["status"] == "SEALED"
    second = _seal(tmp_path, source)
    assert second["decision"] == "BLOCKED_DATA"
    naive = _sealer()(source, tmp_path / "other", "btc_eth_aggressive", AS_OF.replace(tzinfo=None), START, AS_OF)
    assert naive["decision"] == "BLOCKED_DATA"


def test_replay_private_files_are_git_ignored():
    repo = Path(__file__).resolve().parents[3]
    candidate = repo / "var/replay/snapshots/hrs/20261022T000000Z/manifest.json"
    process = subprocess.run(["git", "check-ignore", "-q", str(candidate)], cwd=repo, check=False)
    assert process.returncode == 0


def test_cli_only_seals_local_files_and_reports_unverified(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    source = _source(tmp_path)
    output = tmp_path / "output"
    process = subprocess.run(
        [
            "python3", "scripts/export_replay_snapshot.py", "--source", str(source),
            "--output-root", str(output), "--strategy", "btc_eth_aggressive",
            "--as-of", "2026-10-22T00:00:00Z", "--window-start", "2026-10-21T20:00:00Z",
            "--window-end", "2026-10-22T00:00:00Z",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["status"] == "SEALED"
    assert result["source_provenance"] == "unverified"
