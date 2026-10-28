"""XAU 影子实例运行态就绪探针测试。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


EXPECTED_ENV = {
    "INSTANCE_ID": "mtcps_xau_calibrated_shadow",
    "SHADOW_PROFILES": "xau_calibrated",
    "SHADOW_RUN_ID_PREFIX": "xau-calibrated-shadow",
    "SHADOW_CRON": "20 * * * *",
    "EXECUTION_MODE": "shadow_batch",
    "REAL_ORDERING_ENABLED": "false",
    "STATE_REPORTING_ENABLED": "false",
}


def _valid_payload() -> dict:
    return {
        "pid": os.getpid(),
        "process_start_token": "start-123",
        "instance_id": "mtcps_xau_calibrated_shadow",
        "profiles": ["xau_calibrated"],
        "run_id": "xau-calibrated-shadow-20261028080000-deadbeef",
        "run_id_prefix": "xau-calibrated-shadow",
        "cron": "20 * * * *",
        "execution_mode": "shadow_batch",
        "real_ordering_enabled": False,
        "state_reporting_enabled": False,
    }


def test_atomic_write_and_clear_readiness_file(tmp_path):
    from strategies.btc_eth.shadow.readiness import (
        clear_readiness,
        write_readiness,
    )

    path = tmp_path / "shadow-ready.json"
    write_readiness(_valid_payload(), path=path)

    assert json.loads(path.read_text(encoding="utf-8")) == _valid_payload()
    assert list(tmp_path.glob(".shadow-ready.json.*")) == []

    clear_readiness(path=path)
    assert not path.exists()


def test_probe_accepts_live_matching_runtime(tmp_path, monkeypatch):
    from strategies.btc_eth.shadow import readiness

    path = tmp_path / "shadow-ready.json"
    path.write_text(json.dumps(_valid_payload()), encoding="utf-8")

    monkeypatch.setattr(readiness, "process_start_token", lambda pid: "start-123")
    ok, reason = readiness.check_readiness(path=path, environ=EXPECTED_ENV)

    assert ok is True
    assert reason == "ready"


@pytest.mark.parametrize(
    ("field", "mutated"),
    [
        ("pid", 99999999),
        ("process_start_token", "stale-start-token"),
        ("instance_id", "mtcps_xau_shadow"),
        ("profiles", ["xau_original", "xau_aggressive"]),
        ("run_id", "xau-shadow-wrong-prefix"),
        ("run_id_prefix", "xau-shadow"),
        ("cron", "15 * * * *"),
        ("execution_mode", "live"),
        ("real_ordering_enabled", True),
        ("state_reporting_enabled", True),
    ],
)
def test_probe_rejects_runtime_contract_mutations(
    tmp_path,
    field,
    mutated,
    monkeypatch,
):
    from strategies.btc_eth.shadow import readiness

    payload = _valid_payload()
    payload[field] = mutated
    path = tmp_path / "shadow-ready.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(readiness, "process_start_token", lambda pid: "start-123")
    ok, reason = readiness.check_readiness(path=path, environ=EXPECTED_ENV)

    assert ok is False
    assert field in reason or field == "pid"


def test_probe_rejects_reused_pid_with_stale_start_token(tmp_path, monkeypatch):
    from strategies.btc_eth.shadow import readiness

    path = tmp_path / "shadow-ready.json"
    path.write_text(json.dumps(_valid_payload()), encoding="utf-8")
    monkeypatch.setattr(readiness, "process_start_token", lambda pid: "start-456")

    ok, reason = readiness.check_readiness(path=path, environ=EXPECTED_ENV)

    assert ok is False
    assert "process_start_token" in reason


def test_process_start_token_reads_linux_proc_stat(tmp_path):
    from strategies.btc_eth.shadow.readiness import process_start_token

    pid = 42
    proc_dir = tmp_path / str(pid)
    proc_dir.mkdir()
    # fields 1/2 precede the tail; tail[19] is Linux proc stat field 22 (starttime).
    tail = ["S", *[str(index) for index in range(4, 22)], "987654", "0"]
    (proc_dir / "stat").write_text(
        f"{pid} (worker with spaces) {' '.join(tail)}\n", encoding="utf-8"
    )

    assert process_start_token(pid, proc_root=tmp_path) == "987654"


def test_probe_rejects_missing_or_invalid_marker(tmp_path):
    from strategies.btc_eth.shadow.readiness import check_readiness

    path = tmp_path / "shadow-ready.json"
    ok, _ = check_readiness(path=path, environ=EXPECTED_ENV)
    assert ok is False

    path.write_text("not-json", encoding="utf-8")
    ok, _ = check_readiness(path=path, environ=EXPECTED_ENV)
    assert ok is False


def test_probe_cli_returns_nonzero_for_failed_contract(tmp_path, monkeypatch):
    from strategies.btc_eth.shadow import readiness

    path = tmp_path / "shadow-ready.json"
    path.write_text(json.dumps(_valid_payload()), encoding="utf-8")
    monkeypatch.setenv("SHADOW_READINESS_FILE", str(path))
    for key, value in EXPECTED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("REAL_ORDERING_ENABLED", "true")

    assert readiness.main() != 0
