"""Task 6: frozen live golden snapshots versus frozen replay output."""

import json
from copy import deepcopy
from decimal import Decimal
import pytest

from ai_tuner.parity.golden import compare_golden


CONFIG = {
    "price_tolerance_bps": "10",
    "time_tolerance_seconds": 2,
    "pnl_tolerance_usdt": "0.10",
    "min_golden_trades": 1,
}


def snapshot(source_kind="live"):
    return {
        "snapshot_ref": "sha256:frozen-live-2026-10-01",
        "source_kind": source_kind,
        "is_synthetic": source_kind != "live",
        "as_of": "2026-10-01T02:00:00Z",
        "trades": [{
            "trade_id": "trade-1", "direction": "LONG",
            "signal_time": "2026-10-01T00:00:00Z", "signal_price": "100",
            "entry_time": "2026-10-01T00:00:01Z", "entry_price": "100",
            "exit_time": "2026-10-01T01:00:00Z", "exit_price": "110",
            "exit_reason": "TP1", "realized_pnl": "10", "fee_paid": "0.05",
            "slippage_cost": "0", "funding_fee": "0", "net_pnl": "9.95",
        }],
        "equity_curve": [
            {"ts": "2026-10-01T00:00:00Z", "realized_equity": "1000", "marked_equity": "1000"},
            {"ts": "2026-10-01T01:00:00Z", "realized_equity": "1009.95", "marked_equity": "1009.95"},
        ],
    }


def compare(tmp_path, golden=None, replay=None, config=CONFIG):
    golden = snapshot() if golden is None else golden
    replay = deepcopy(golden) if replay is None else replay
    golden_path = tmp_path / "golden.json"
    replay_path = tmp_path / "replay.json"
    golden_path.write_text(json.dumps(golden), encoding="utf-8")
    replay_path.write_text(json.dumps(replay), encoding="utf-8")
    return compare_golden(golden_path, replay_path, config)


def test_live_labeled_snapshot_matches_with_configured_tolerances(tmp_path):
    replay = snapshot()
    replay["trades"][0]["signal_time"] = "2026-10-01T00:00:02Z"
    replay["trades"][0]["entry_price"] = "100.10"
    replay["trades"][0]["fee_paid"] = "0.15"
    replay["trades"][0]["net_pnl"] = "10.05"
    replay["equity_curve"][1]["marked_equity"] = "1010.05"
    report = compare(tmp_path, replay=replay)
    assert report["parity_status"] == "match"
    assert report["status"] == "PASS"
    assert report["golden_snapshot_ref"] == "sha256:frozen-live-2026-10-01"
    assert report["sample_count"] == 1
    assert all(item["pass"] for item in report["compares"])
    assert report["tolerance"] == {key: CONFIG[key] for key in CONFIG if key != "min_golden_trades"}


@pytest.mark.parametrize("path,value", [
    (("trades", 0, "direction"), "SHORT"),
    (("trades", 0, "signal_time"), "2026-10-01T00:00:03Z"),
    (("trades", 0, "signal_price"), "100.11"),
    (("trades", 0, "entry_time"), "2026-10-01T00:00:04Z"),
    (("trades", 0, "entry_price"), "100.11"),
    (("trades", 0, "exit_time"), "2026-10-01T01:00:03Z"),
    (("trades", 0, "exit_price"), "110.12"),
    (("trades", 0, "exit_reason"), "STOP_LOSS"),
    (("trades", 0, "realized_pnl"), "10.11"),
    (("trades", 0, "fee_paid"), "0.16"),
    (("trades", 0, "slippage_cost"), "0.11"),
    (("trades", 0, "funding_fee"), "0.11"),
    (("trades", 0, "net_pnl"), "10.06"),
    (("equity_curve", 1, "realized_equity"), "1010.06"),
    (("equity_curve", 1, "marked_equity"), "1010.06"),
])
def test_each_mismatched_field_fails(tmp_path, path, value):
    replay = snapshot()
    target = replay
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    report = compare(tmp_path, replay=replay)
    assert report["parity_status"] == "mismatch"
    assert report["status"] == "FAIL"
    assert any(not item["pass"] and item["field"].endswith(path[-1]) for item in report["compares"])


def test_sample_shortfall_is_blocked_even_if_values_disagree(tmp_path):
    replay = snapshot()
    replay["trades"][0]["direction"] = "SHORT"
    report = compare(tmp_path, replay=replay, config={**CONFIG, "min_golden_trades": 2})
    assert (report["parity_status"], report["status"]) == ("partial_parity", "BLOCKED_DATA")


@pytest.mark.parametrize("missing", ["price_tolerance_bps", "time_tolerance_seconds", "pnl_tolerance_usdt"])
def test_missing_calibration_is_blocked(tmp_path, missing):
    config = {key: value for key, value in CONFIG.items() if key != missing}
    report = compare(tmp_path, config=config)
    assert (report["parity_status"], report["status"]) == ("partial_parity", "BLOCKED_DATA")


def test_synthetic_fixture_cannot_count_as_real_parity(tmp_path):
    report = compare(tmp_path, golden=snapshot("synthetic"))
    assert (report["parity_status"], report["status"]) == ("partial_parity", "BLOCKED_DATA")
    assert report["sample_count"] == 0


def test_unmarked_fixture_cannot_count_as_real_parity(tmp_path):
    golden = snapshot()
    del golden["is_synthetic"]
    report = compare(tmp_path, golden=golden)
    assert (report["parity_status"], report["status"]) == ("partial_parity", "BLOCKED_DATA")


def test_live_label_does_not_override_synthetic_marker(tmp_path):
    golden = snapshot()
    golden["is_synthetic"] = True
    report = compare(tmp_path, golden=golden)
    assert (report["parity_status"], report["status"]) == ("partial_parity", "BLOCKED_DATA")
    assert report["sample_count"] == 0


def test_missing_as_of_blocks_baseline(tmp_path):
    golden = snapshot()
    del golden["as_of"]
    report = compare(tmp_path, golden=golden)
    assert (report["parity_status"], report["status"]) == ("partial_parity", "BLOCKED_DATA")


def test_replay_event_after_as_of_fails_even_when_both_snapshots_agree(tmp_path):
    golden = snapshot()
    golden["trades"][0]["exit_time"] = "2026-10-01T03:00:00Z"
    report = compare(tmp_path, golden=golden)
    assert (report["parity_status"], report["status"]) != ("match", "PASS")


@pytest.mark.parametrize("location", ["golden", "replay"])
def test_missing_trade_field_never_passes(tmp_path, location):
    golden = snapshot()
    replay = snapshot()
    (golden if location == "golden" else replay)["trades"][0].pop("entry_price")
    report = compare(tmp_path, golden=golden, replay=replay)
    assert (report["parity_status"], report["status"]) == ("mismatch", "FAIL")


@pytest.mark.parametrize("config", [
    {**CONFIG, "price_tolerance_bps": "NaN"},
    {**CONFIG, "pnl_tolerance_usdt": "-1"},
    {key: value for key, value in CONFIG.items() if key != "min_golden_trades"},
])
def test_invalid_or_missing_config_blocks(tmp_path, config):
    report = compare(tmp_path, config=config)
    assert (report["parity_status"], report["status"]) == ("partial_parity", "BLOCKED_DATA")


def test_decimal_config_produces_json_serializable_report(tmp_path):
    config = {**CONFIG, "price_tolerance_bps": Decimal("10"),
              "pnl_tolerance_usdt": Decimal("0.10")}
    report = compare(tmp_path, config=config)
    assert report["status"] == "PASS"
    assert json.loads(json.dumps(report))["tolerance"]["price_tolerance_bps"] == "10"


@pytest.mark.parametrize("config", [None, [], "invalid"])
def test_non_mapping_config_blocks_without_exception(tmp_path, config):
    report = compare(tmp_path, config=config)
    assert (report["parity_status"], report["status"]) == ("partial_parity", "BLOCKED_DATA")


@pytest.mark.parametrize("payload", [None, "{broken", "[]"])
def test_missing_or_corrupt_replay_fails_after_real_sample_gate(tmp_path, payload):
    golden_path = tmp_path / "golden.json"
    replay_path = tmp_path / "replay.json"
    golden_path.write_text(json.dumps(snapshot()), encoding="utf-8")
    if payload is not None:
        replay_path.write_text(payload, encoding="utf-8")
    report = compare_golden(golden_path, replay_path, CONFIG)
    assert (report["parity_status"], report["status"]) == ("mismatch", "FAIL")
    assert any(not item["pass"] for item in report["compares"])


@pytest.mark.parametrize("payload", ["{broken", "[]"])
def test_corrupt_golden_blocks_without_exception(tmp_path, payload):
    golden_path = tmp_path / "golden.json"
    replay_path = tmp_path / "replay.json"
    golden_path.write_text(payload, encoding="utf-8")
    replay_path.write_text(json.dumps(snapshot()), encoding="utf-8")
    report = compare_golden(golden_path, replay_path, CONFIG)
    assert (report["parity_status"], report["status"]) == ("partial_parity", "BLOCKED_DATA")


@pytest.mark.parametrize("section", ["trades", "equity_curve"])
@pytest.mark.parametrize("bad_value", [None, {}, [None], ["bad row"]])
def test_malformed_golden_shape_blocks(tmp_path, section, bad_value):
    golden = snapshot()
    golden[section] = bad_value
    report = compare(tmp_path, golden=golden)
    assert (report["parity_status"], report["status"]) == ("partial_parity", "BLOCKED_DATA")


@pytest.mark.parametrize("section", ["trades", "equity_curve"])
@pytest.mark.parametrize("bad_value", [None, {}, [None], ["bad row"]])
def test_malformed_replay_shape_fails(tmp_path, section, bad_value):
    replay = snapshot()
    replay[section] = bad_value
    report = compare(tmp_path, replay=replay)
    assert (report["parity_status"], report["status"]) == ("mismatch", "FAIL")


def test_mismatched_trade_cardinality_fails_after_real_sample_gate(tmp_path):
    replay = snapshot()
    replay["trades"] = []
    report = compare(tmp_path, replay=replay)
    assert (report["parity_status"], report["status"]) == ("mismatch", "FAIL")


def test_missing_golden_file_is_no_baseline(tmp_path):
    replay_path = tmp_path / "replay.json"
    replay_path.write_text(json.dumps(snapshot()), encoding="utf-8")
    report = compare_golden(tmp_path / "missing.json", replay_path, CONFIG)
    assert (report["parity_status"], report["status"]) == ("no_baseline", "BLOCKED_DATA")
