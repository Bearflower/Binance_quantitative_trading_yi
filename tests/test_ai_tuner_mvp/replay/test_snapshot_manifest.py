"""Task 1：冻结输入快照的 manifest 校验（fail-closed）。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from ai_tuner.replay.data_portal import DataPortal, compute_manifest_sha256, sha256_file
from ai_tuner.replay.models import DataBlocked

from .conftest import make_bars, write_snapshot

UTC = timezone.utc
AS_OF = datetime(2026, 10, 22, tzinfo=UTC)
WINDOW_START = AS_OF - timedelta(hours=4)


def _build(tmp_path, **kwargs):
    prices = [
        ("100", "101", "99", "100.5"),
        ("100.5", "102", "100", "101.5"),
        ("101.5", "103", "101", "102.5"),
        ("102.5", "103.5", "98", "99"),
    ]
    bars = {"BTCUSDT|1h": make_bars("BTCUSDT", "1h", WINDOW_START, prices)}
    directory = str(tmp_path / "snapshot")
    top = write_snapshot(
        directory,
        "btc_eth_aggressive",
        AS_OF,
        WINDOW_START,
        bars,
        ["BTCUSDT"],
        ["1h"],
        funding_by_symbol={"BTCUSDT": [(WINDOW_START + timedelta(hours=2), "0.0001")]},
        **kwargs
    )
    return directory, top


def _portal(directory):
    return DataPortal(directory, "btc_eth_aggressive", AS_OF, WINDOW_START)


def test_manifest_hash_is_stable_and_reproducible(tmp_path):
    directory, top = _build(tmp_path)
    first = _portal(directory).manifest_sha256
    second = _portal(directory).manifest_sha256
    assert first == top
    assert first == second

    manifest = json.load(open(os.path.join(directory, "manifest.json"), encoding="utf-8"))
    assert compute_manifest_sha256(manifest["files"]) == top


def test_missing_kline_file_is_blocked_not_ignored(tmp_path):
    directory, _ = _build(tmp_path, skip_files=["klines/BTCUSDT_1h.jsonl"])
    with pytest.raises(DataBlocked) as excinfo:
        _portal(directory).bars("BTCUSDT", "1h")
    assert "file_missing" in " ".join(excinfo.value.gaps)


def test_missing_manifest_is_blocked(tmp_path):
    directory = str(tmp_path / "empty")
    os.makedirs(directory)
    with pytest.raises(DataBlocked) as excinfo:
        DataPortal(directory, "btc_eth_aggressive", AS_OF, WINDOW_START)
    assert "manifest_missing" in " ".join(excinfo.value.gaps)


def test_tampered_file_content_is_detected(tmp_path):
    directory, _ = _build(tmp_path)
    target = os.path.join(directory, "klines/BTCUSDT_1h.jsonl")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write('{"symbol":"BTCUSDT","interval":"1h"}\n')
    with pytest.raises(DataBlocked) as excinfo:
        _portal(directory)
    assert "snapshot_tampered" in " ".join(excinfo.value.gaps)


def test_nested_hash_tamper_is_caught_by_top_level_hash(tmp_path):
    directory, _ = _build(tmp_path)
    manifest_path = os.path.join(directory, "manifest.json")
    manifest = json.load(open(manifest_path, encoding="utf-8"))

    target = os.path.join(directory, "klines/BTCUSDT_1h.jsonl")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write('{"symbol":"BTCUSDT","interval":"1h"}\n')
    # 同步篡改嵌套 sha256，但保留旧顶层哈希
    manifest["files"]["klines/BTCUSDT_1h.jsonl"]["sha256"] = sha256_file(target)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, sort_keys=True)

    with pytest.raises(DataBlocked) as excinfo:
        _portal(directory)
    assert "snapshot_manifest_hash_mismatch" in " ".join(excinfo.value.gaps)
