"""Task 10：六个不可变报告的 canonical 序列化与状态一致性。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ai_tuner.replay.artifacts import (
    assert_status_consistent,
    canonical_json,
    to_jsonable,
    write_bundle,
)

UTC = timezone.utc


def _reports(status):
    return {
        "run_manifest": {"status": status, "engine_version": "e1", "as_of": datetime(2026, 10, 22, tzinfo=UTC)},
        "data_quality": {"status": status, "data_complete": True, "future_data_violations": 0},
        "parity_report": {"status": status, "parity_status": "match", "sample_count": 0},
        "scorecard": {"status": status, "net_pnl_after_costs": Decimal("12.50")},
        "qualification_decision": {"status": status, "gates_total": 10},
    }


def test_canonical_json_is_sorted_compact_and_decimal_is_string():
    text = canonical_json({"b": Decimal("1.50"), "a": [Decimal("2"), "x"]})
    assert text == '{"a":["2","x"],"b":"1.50"}\n'


def test_to_jsonable_handles_datetime_and_tuple():
    payload = to_jsonable(
        {"ts": datetime(2026, 10, 22, 3, tzinfo=UTC), "items": (Decimal("1"),)}
    )
    assert payload == {"ts": "2026-10-22T03:00:00Z", "items": ["1"]}


def test_status_mismatch_is_rejected():
    reports = _reports("PASS")
    reports["scorecard"]["status"] = "FAIL"
    with pytest.raises(ValueError):
        assert_status_consistent(reports)


def test_bundle_is_byte_identical_on_repeat(tmp_path):
    first = write_bundle(tmp_path / "run1", _reports("PASS"), [{"ts": "2026-10-22T00:00:00Z", "seq": 0, "type": "run_start", "payload": {}}])
    second = write_bundle(tmp_path / "run2", _reports("PASS"), [{"ts": "2026-10-22T00:00:00Z", "seq": 0, "type": "run_start", "payload": {}}])

    assert sorted(first) == sorted(second)
    for name in first:
        with open(first[name], "rb") as handle:
            left = handle.read()
        with open(second[name], "rb") as handle:
            right = handle.read()
        assert left == right, name


def test_written_report_contains_single_trailing_newline(tmp_path):
    written = write_bundle(tmp_path, _reports("BLOCKED_DATA"), [])
    with open(written["scorecard.json"], "rb") as handle:
        raw = handle.read()
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    payload = json.loads(raw.decode("utf-8"))
    assert payload["net_pnl_after_costs"] == "12.50"
    assert payload["status"] == "BLOCKED_DATA"
    # audit 文件在无事件时必须存在（空文件由 engine 保证至少 run_start）
    assert os.path.getsize(written["audit_events.jsonl"]) == 0
