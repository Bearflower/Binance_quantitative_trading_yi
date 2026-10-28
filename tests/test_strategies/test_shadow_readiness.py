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


def test_probe_accepts_live_matching_runtime(tmp_path):
    from strategies.btc_eth.shadow.readiness import check_readiness

    path = tmp_path / "shadow-ready.json"
    path.write_text(json.dumps(_valid_payload()), encoding="utf-8")

    ok, reason = check_readiness(path=path, environ=EXPECTED_ENV)

    assert ok is True
    assert reason == "ready"


@pytest.mark.parametrize(
    ("field", "mutated"),
    [
        ("pid", 99999999),
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
):
    from strategies.btc_eth.shadow.readiness import check_readiness

    payload = _valid_payload()
    payload[field] = mutated
    path = tmp_path / "shadow-ready.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    ok, reason = check_readiness(path=path, environ=EXPECTED_ENV)

    assert ok is False
    assert field in reason or field == "pid"


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
