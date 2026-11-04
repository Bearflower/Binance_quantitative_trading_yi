"""Calibrated-only replay must not depend on original/aggressive profile state."""
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone

import yaml

from shared.config_loader import compute_config_hash
from backtest.btc_eth.replay.engine import ReplayEngine
from backtest.btc_eth.replay.report import write_reports
from backtest.btc_eth.replay import main as replay_main
from backtest.btc_eth.replay.snapshot import _compute_manifest_sha256
from tests.test_shadow_replay.test_replay_determinism import _make_fixture_dir


ROOT = Path(__file__).resolve().parents[2]
CALIBRATED = ROOT / "strategies/btc_eth/shadow/config/xau_calibrated.yaml"


def test_calibrated_market_coverage_rejects_interior_minute_gap_and_missing_funding():
    start = datetime(2026, 10, 1, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=13)
    bars = [{"open_time": int((start + timedelta(minutes=i)).timestamp() * 1000),
             "close_time": int((start + timedelta(minutes=i + 1)).timestamp() * 1000) - 1}
            for i in range(13 * 60)]
    funding = [(start + timedelta(hours=4), "0.0001"),
               (start + timedelta(hours=8), "0.0001"),
               (start + timedelta(hours=12), "0.0001")]
    assert replay_main.validate_calibrated_market_data(bars, funding, start, end) == []
    assert "minute_gap" in replay_main.validate_calibrated_market_data(
        bars[:30] + bars[31:], funding, start, end)
    assert "funding_missing" in replay_main.validate_calibrated_market_data(bars, [], start, end)
    assert "funding_gap" in replay_main.validate_calibrated_market_data(
        bars, [funding[0]], start, end)


def test_calibrated_only_engine_runs_with_frozen_config(tmp_path):
    fixture = _make_fixture_dir()
    manifest_path = Path(fixture["run_manifest_path"])
    manifest = json.loads(manifest_path.read_text())
    manifest["run_id"] = "xau-calibrated-test"
    manifest["instance_id"] = "mtcps_xau_calibrated_shadow"
    manifest["config_hashes"] = {"xau_calibrated": compute_config_hash(yaml.safe_load(CALIBRATED.read_text()))}
    events_path = Path(fixture["events_path"])
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    events = [row for row in events if row["profile_id"] == "xau_original"]
    for row in events:
        row["profile_id"] = "xau_calibrated"
        row["run_id"] = manifest["run_id"]
        row["config_hash"] = manifest["config_hashes"]["xau_calibrated"]
    first = min(events, key=lambda row: row["scheduled_at"])
    first.update({"decision": "candidate", "direction": "LONG", "grade": "A",
                  "reference_price": "2000", "intended_order_type": "MARKET",
                  "intended_entry_price": "2000", "stop_loss_rule": {"atr_value": "10"}})
    events_path.write_text("".join(json.dumps(row) + "\n" for row in events))

    engine = ReplayEngine(
        replay_config_path=str(ROOT / "backtest/btc_eth/replay/config.yaml"),
        profile_config_path_original="",
        profile_config_path_aggressive="",
        klines_1m_path=fixture["klines_path"],
        funding_rates_path=fixture["funding_path"],
        exchange_info_json_path=fixture["ei_path"],
        snapshot_manifest_path=fixture["manifest_path"],
        run_manifest=manifest,
        profile_config_path_calibrated=str(CALIBRATED),
    )
    engine.load_events(str(events_path))
    result = engine.run()
    assert "xau_calibrated" in result
    assert "xau_original" not in result
    assert result["engine_metadata"]["decision_summary"]["per_profile"]["xau_calibrated"]["scheduled_runs"] == 10
    assert any(trade.get("filled") for trade in result["xau_calibrated"]["trades"])
    outputs = write_reports(
        engine_result=result, run_manifest=manifest, output_dir=str(tmp_path),
        data_completeness={"gaps": 0, "complete": True, "decision_missing_hours": ["2026-10-01T11:00:00+00:00"]},
    )
    report = json.loads(Path(outputs["json_path"]).read_text())
    assert "xau_calibrated" in report
    assert report["xau_calibrated"]["n_closed_trades"] >= 1
    assert report["admission"]["admission"] == "insufficient_data"
    assert report["data_completeness"]["decision_missing_hours"] == ["2026-10-01T11:00:00+00:00"]

    events_path.write_text(json.dumps(first) + "\n")
    manifest["data_cutoff_at"] = "2026-10-01T11:00:00+00:00"
    coverage_path = Path(fixture["dir"]) / "coverage.json"
    coverage_path.write_text(json.dumps({
        "profile_id": "xau_calibrated", "symbol": "XAUUSDT",
        "window_start": "2026-10-01T10:00:00+00:00",
        "window_end": "2026-10-01T11:00:00+00:00",
        "expected_hours": 1, "events_written": 1,
        "missing_hours": [],
        "quality_degraded_hours": [],
    }))
    manifest["coverage_sha256"] = hashlib.sha256(coverage_path.read_bytes()).hexdigest()
    manifest["decision_events_sha256"] = hashlib.sha256(events_path.read_bytes()).hexdigest()
    funding_path = Path(fixture["funding_path"])
    funding_path.write_text(json.dumps([{
        "fundingTime": int(datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc).timestamp() * 1000),
        "fundingRate": "0.0001",
    }]))
    snapshot_manifest_path = Path(fixture["manifest_path"])
    snapshot_manifest = json.loads(snapshot_manifest_path.read_text())
    snapshot_manifest["files"]["funding_rates"]["sha256"] = hashlib.sha256(funding_path.read_bytes()).hexdigest()
    snapshot_manifest["files"]["funding_rates"]["rows"] = 1
    snapshot_manifest["sha256"] = _compute_manifest_sha256(snapshot_manifest["files"])
    snapshot_manifest_path.write_text(json.dumps(snapshot_manifest))
    manifest_path.write_text(json.dumps(manifest))
    cli_dir = tmp_path / "cli"
    assert replay_main.main([
        "--events", str(events_path), "--run-manifest", str(manifest_path),
        "--coverage", str(coverage_path), "--snapshot-dir", fixture["dir"],
        "--profile-calibrated", str(CALIBRATED), "--output-dir", str(cli_dir),
    ]) == 0
    cli_report = json.loads((cli_dir / "report_xau-calibrated-test.json").read_text())
    assert cli_report["admission"]["admission"] == "insufficient_data"
    assert cli_report["xau_calibrated"]["n_closed_trades"] >= 1

    # 文件及顶层哈希均有效时，内部缺分钟不能被伪造的 gaps=[] 掩盖。
    kline_path = Path(fixture["klines_path"])
    original_klines = kline_path.read_text()
    rows = original_klines.splitlines()
    kline_path.write_text("\n".join(rows[:30] + rows[31:]) + "\n")
    snapshot_manifest["files"]["klines_1m"]["sha256"] = hashlib.sha256(kline_path.read_bytes()).hexdigest()
    snapshot_manifest["files"]["klines_1m"]["rows"] -= 1
    snapshot_manifest["sha256"] = _compute_manifest_sha256(snapshot_manifest["files"])
    snapshot_manifest_path.write_text(json.dumps(snapshot_manifest))
    assert replay_main.main([
        "--events", str(events_path), "--run-manifest", str(manifest_path),
        "--coverage", str(coverage_path), "--snapshot-dir", fixture["dir"],
        "--profile-calibrated", str(CALIBRATED), "--output-dir", str(tmp_path / "interior-gap"),
    ]) == 2
    assert not (tmp_path / "interior-gap").exists()
    kline_path.write_text(original_klines)
    snapshot_manifest["files"]["klines_1m"]["sha256"] = hashlib.sha256(kline_path.read_bytes()).hexdigest()
    snapshot_manifest["files"]["klines_1m"]["rows"] += 1
    snapshot_manifest["sha256"] = _compute_manifest_sha256(snapshot_manifest["files"])
    snapshot_manifest_path.write_text(json.dumps(snapshot_manifest))

    funding_path.write_text("[]")
    snapshot_manifest["files"]["funding_rates"]["sha256"] = hashlib.sha256(funding_path.read_bytes()).hexdigest()
    snapshot_manifest["files"]["funding_rates"]["rows"] = 0
    snapshot_manifest["sha256"] = _compute_manifest_sha256(snapshot_manifest["files"])
    snapshot_manifest_path.write_text(json.dumps(snapshot_manifest))
    assert replay_main.main([
        "--events", str(events_path), "--run-manifest", str(manifest_path),
        "--coverage", str(coverage_path), "--snapshot-dir", fixture["dir"],
        "--profile-calibrated", str(CALIBRATED), "--output-dir", str(tmp_path / "no-funding"),
    ]) == 2
    assert not (tmp_path / "no-funding").exists()

    # 即使 coverage 声称小时缺口已记录，1m 快照未覆盖窗口也必须拒绝。
    longer_coverage = json.loads(coverage_path.read_text())
    longer_coverage.update({"window_end": "2026-10-01T12:00:00+00:00",
                            "expected_hours": 2,
                            "missing_hours": ["2026-10-01T11:00:00+00:00"]})
    coverage_path.write_text(json.dumps(longer_coverage))
    manifest["coverage_sha256"] = hashlib.sha256(coverage_path.read_bytes()).hexdigest()
    manifest["data_cutoff_at"] = longer_coverage["window_end"]
    manifest_path.write_text(json.dumps(manifest))
    assert replay_main.main([
        "--events", str(events_path), "--run-manifest", str(manifest_path),
        "--coverage", str(coverage_path), "--snapshot-dir", fixture["dir"],
        "--profile-calibrated", str(CALIBRATED),
        "--output-dir", str(tmp_path / "truncated"),
    ]) == 2
    assert not (tmp_path / "truncated").exists()

    # 全部决策缺失仍应可产出「样本不足」，不能把零交易误判为通过。
    events_path.write_text("")
    empty_engine = ReplayEngine(
        replay_config_path=str(ROOT / "backtest/btc_eth/replay/config.yaml"),
        profile_config_path_original="", profile_config_path_aggressive="",
        klines_1m_path=fixture["klines_path"], funding_rates_path=fixture["funding_path"],
        exchange_info_json_path=fixture["ei_path"], snapshot_manifest_path=fixture["manifest_path"],
        run_manifest=manifest, profile_config_path_calibrated=str(CALIBRATED),
    )
    empty_engine.load_events(str(events_path))
    empty_result = empty_engine.run()
    assert empty_result["engine_metadata"]["decision_summary"]["per_profile"]["xau_calibrated"]["scheduled_runs"] == 0
    assert empty_result["xau_calibrated"]["trades"] == []
