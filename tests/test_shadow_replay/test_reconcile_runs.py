"""离线 XAU 校准版跨 run 汇总：只读输入、冲突拒绝、缺口显式化。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from shared.config_loader import compute_config_hash
from strategies.btc_eth.shadow.atr_calibration import _manifest_hash
from backtest.btc_eth.replay.reconcile_runs import reconcile_run_exports


def _evidence(root: Path) -> tuple[Path, Path, Path, str]:
    snapshot = root / "snapshot.jsonl"
    snapshot.write_bytes(b'{"close_time":1}\n')
    snap_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    manifest = {"snapshot_sha256": snap_hash, "symbol": "XAUUSDT"}
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    manifest_path = root / "calibration_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    cfg = {
        "instance": {"initial_virtual_equity_usdt": 300},
        "shadow": {
            "calibration_manifest_sha256": manifest["manifest_sha256"],
            "calibration_snapshot_sha256": snap_hash,
        },
    }
    cfg_path = root / "xau_calibrated.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return cfg_path, manifest_path, snapshot, compute_config_hash(cfg)


def _run(root: Path, run_id: str, cfg_hash: str, events: list[dict]) -> Path:
    path = root / run_id
    path.mkdir()
    manifest = {
        "run_id": run_id,
        "instance_id": "mtcps_xau_calibrated_shadow",
        "symbol": "XAUUSDT",
        "started_at": "2026-11-02T03:00:00+00:00",
        "ended_at": "2026-11-02T09:00:00+00:00",
        "status": "closed",
        "initial_virtual_equity_usdt": 300,
        "git_commit": run_id,
        "git_sha": run_id,
        "config_hashes": {"xau_calibrated": cfg_hash},
    }
    (path / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with (path / "decision_events.jsonl").open("w", encoding="utf-8") as stream:
        for event in events:
            row = {
                "run_id": run_id,
                "profile_id": "xau_calibrated",
                "symbol": "XAUUSDT",
                "scheduled_at": event["scheduled_at"],
                "evaluated_at": event["evaluated_at"],
                "decision": event.get("decision", "no_signal"),
                "config_hash": cfg_hash,
                "git_commit": run_id,
                "source_kline_close_times": {"1h": 1, "4h": 1, "1d": 1},
            }
            stream.write(json.dumps(row) + "\n")
    return path


def test_reconcile_identical_cross_run_event_and_marks_real_gap(tmp_path: Path):
    cfg, manifest, snapshot, cfg_hash = _evidence(tmp_path)
    first = _run(tmp_path, "run-a", cfg_hash, [
        {"scheduled_at": "2026-11-02T03:00:00+00:00", "evaluated_at": "2026-11-02T03:20:00+00:00"},
        {"scheduled_at": "2026-11-02T08:00:00+00:00", "evaluated_at": "2026-11-02T08:20:00+00:00"},
    ])
    second = _run(tmp_path, "run-b", cfg_hash, [
        {"scheduled_at": "2026-11-02T08:00:00+00:00", "evaluated_at": "2026-11-02T08:29:00+00:00"},
    ])

    result = reconcile_run_exports(
        [first, second], cfg, manifest, snapshot,
        "2026-11-02T03:00:00+00:00", "2026-11-02T09:00:00+00:00",
        tmp_path / "out",
    )

    assert result["events_written"] == 2
    assert result["duplicates_removed"] == 1
    assert result["duplicate_events"] == [{
        "scheduled_at": "2026-11-02T08:00:00+00:00",
        "chosen_run_id": "run-a",
        "source_run_ids": ["run-a", "run-b"],
    }]
    assert result["missing_hours"] == [
        f"2026-11-02T{hour:02d}:00:00+00:00" for hour in range(4, 8)
    ]
    rows = [json.loads(line) for line in (tmp_path / "out" / "decision_events.jsonl").read_text().splitlines()]
    assert [row["run_id"] for row in rows] == ["run-a", "run-a"]
    reconcile_run_exports([second, first], cfg, manifest, snapshot,
                          "2026-11-02T03:00:00+00:00", "2026-11-02T09:00:00+00:00",
                          tmp_path / "out-again")
    for name in ("decision_events.jsonl", "coverage.json", "run_manifest.json"):
        assert (tmp_path / "out" / name).read_bytes() == (tmp_path / "out-again" / name).read_bytes()


def test_reconcile_conflicting_duplicate_fails_closed(tmp_path: Path):
    cfg, manifest, snapshot, cfg_hash = _evidence(tmp_path)
    first = _run(tmp_path, "run-a", cfg_hash, [
        {"scheduled_at": "2026-11-02T08:00:00+00:00", "evaluated_at": "2026-11-02T08:20:00+00:00"},
    ])
    second = _run(tmp_path, "run-b", cfg_hash, [
        {"scheduled_at": "2026-11-02T08:00:00+00:00", "evaluated_at": "2026-11-02T08:29:00+00:00", "decision": "candidate"},
    ])

    with pytest.raises(ValueError, match="conflicting_duplicate"):
        reconcile_run_exports(
            [first, second], cfg, manifest, snapshot,
            "2026-11-02T08:00:00+00:00", "2026-11-02T09:00:00+00:00",
            tmp_path / "out",
        )
    assert not (tmp_path / "out").exists()


def test_reconcile_rejects_wrong_frozen_config_and_missing_run(tmp_path: Path):
    cfg, manifest, snapshot, cfg_hash = _evidence(tmp_path)
    first = _run(tmp_path, "run-a", cfg_hash, [
        {"scheduled_at": "2026-11-02T08:00:00+00:00", "evaluated_at": "2026-11-02T08:20:00+00:00"},
    ])
    config = yaml.safe_load(cfg.read_text())
    config["instance"]["initial_virtual_equity_usdt"] = 301
    cfg.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="calibrated_initial_equity_must_be_300"):
        reconcile_run_exports([first], cfg, manifest, snapshot,
                              "2026-11-02T08:00:00+00:00", "2026-11-02T09:00:00+00:00", tmp_path / "out")
    assert not (tmp_path / "out").exists()

    config["instance"]["initial_virtual_equity_usdt"] = 300
    cfg.write_text(yaml.safe_dump(config))
    with pytest.raises(FileNotFoundError):
        reconcile_run_exports([tmp_path / "absent"], cfg, manifest, snapshot,
                              "2026-11-02T08:00:00+00:00", "2026-11-02T09:00:00+00:00", tmp_path / "out")


def test_reconcile_rejects_future_source_bar(tmp_path: Path):
    cfg, manifest, snapshot, cfg_hash = _evidence(tmp_path)
    run = _run(tmp_path, "run-a", cfg_hash, [
        {"scheduled_at": "2026-11-02T08:00:00+00:00", "evaluated_at": "2026-11-02T08:20:00+00:00"},
    ])
    event_path = run / "decision_events.jsonl"
    event = json.loads(event_path.read_text())
    event["source_kline_close_times"]["1h"] = "2026-11-02T09:00:00+00:00"
    event_path.write_text(json.dumps(event) + "\n")
    with pytest.raises(ValueError, match="future_source_kline"):
        reconcile_run_exports([run], cfg, manifest, snapshot,
                              "2026-11-02T08:00:00+00:00", "2026-11-02T09:00:00+00:00", tmp_path / "out")


def test_reconcile_rejects_tampered_calibration_snapshot(tmp_path: Path):
    cfg, manifest, snapshot, cfg_hash = _evidence(tmp_path)
    run = _run(tmp_path, "run-a", cfg_hash, [
        {"scheduled_at": "2026-11-02T08:00:00+00:00", "evaluated_at": "2026-11-02T08:20:00+00:00"},
    ])
    snapshot.write_bytes(snapshot.read_bytes() + b"tampered\n")
    with pytest.raises(ValueError, match="snapshot_sha256"):
        reconcile_run_exports([run], cfg, manifest, snapshot,
                              "2026-11-02T08:00:00+00:00", "2026-11-02T09:00:00+00:00", tmp_path / "out")
    assert not (tmp_path / "out").exists()
