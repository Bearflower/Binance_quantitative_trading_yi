"""Fail-closed local reconciliation of calibrated XAU shadow run exports."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml

from shared.config_loader import compute_config_hash
from strategies.btc_eth.shadow.atr_calibration import verify_manifest


PROFILE = "xau_calibrated"
INSTANCE = "mtcps_xau_calibrated_shadow"


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive_timestamp")
    return parsed.astimezone(timezone.utc)


def _canonical_event(row: Dict[str, Any]) -> str:
    comparable = {key: value for key, value in row.items()
                  if key not in {"run_id", "evaluated_at", "git_commit", "git_sha"}}
    return json.dumps(comparable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _source_close_time(value: Any) -> datetime:
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        numeric = float(value)
        return datetime.fromtimestamp(numeric / 1000 if numeric > 10**11 else numeric, tz=timezone.utc)
    return _time(str(value))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconcile_run_exports(
    run_dirs: Iterable[Path], profile_config_path: Path,
    calibration_manifest_path: Path, calibration_snapshot_path: Path,
    start: str, end: str, output_dir: Path,
) -> Dict[str, Any]:
    """Merge equivalent physical runs into one auditable local decision stream.

    No output is written until all inputs, duplicate semantics and frozen
    calibration evidence have passed validation.
    """
    start_at, end_at = _time(start), _time(end)
    if start_at >= end_at or any(t.minute or t.second or t.microsecond for t in (start_at, end_at)):
        raise ValueError("invalid_hourly_window")
    config_path = Path(profile_config_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("invalid_profile_config")
    config_hash = compute_config_hash(config)
    calibration_path = Path(calibration_manifest_path)
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    snapshot_path = Path(calibration_snapshot_path)
    verify_manifest(calibration, snapshot_path.read_bytes())
    shadow = config.get("shadow", {})
    if (shadow.get("calibration_manifest_sha256") != calibration.get("manifest_sha256")
            or shadow.get("calibration_snapshot_sha256") != calibration.get("snapshot_sha256")
            or calibration.get("symbol") != "XAUUSDT"):
        raise ValueError("calibration_evidence_mismatch")

    dirs = [Path(item) for item in run_dirs]
    if not dirs:
        raise ValueError("no_run_exports")
    seen_runs = set()
    input_hashes: List[Dict[str, str]] = []
    run_segments: List[Dict[str, Any]] = []
    grouped: Dict[datetime, List[Dict[str, Any]]] = {}
    source_missing_hours = set()
    expected_equity = config.get("instance", {}).get("initial_virtual_equity_usdt")
    if expected_equity != 300:
        raise ValueError("calibrated_initial_equity_must_be_300")
    for directory in dirs:
        manifest_path = directory / "run_manifest.json"
        event_path = directory / "decision_events.jsonl"
        run = json.loads(manifest_path.read_text(encoding="utf-8"))
        run_id = run.get("run_id")
        if not run_id or run_id in seen_runs:
            raise ValueError("duplicate_or_missing_run_id")
        seen_runs.add(run_id)
        if (run.get("instance_id") != INSTANCE or run.get("symbol") != "XAUUSDT"
                or run.get("config_hashes") != {PROFILE: config_hash}
                or run.get("initial_virtual_equity_usdt") != expected_equity):
            raise ValueError("run_manifest_mismatch")
        events = []
        seen_within_run = set()
        for line in event_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if (row.get("run_id") != run_id or row.get("profile_id") != PROFILE
                    or row.get("symbol") != "XAUUSDT"
                    or row.get("config_hash") != config_hash):
                raise ValueError("decision_event_mismatch")
            scheduled = _time(row["scheduled_at"])
            evaluated = _time(row["evaluated_at"])
            if scheduled.minute or scheduled.second or scheduled.microsecond or evaluated < scheduled:
                raise ValueError("invalid_event_time")
            if start_at <= scheduled < end_at:
                close_times = row.get("source_kline_close_times") or {}
                if any(close_times.get(period) is None for period in ("1h", "4h", "1d")):
                    source_missing_hours.add(scheduled.isoformat())
                for value in close_times.values():
                    if value is not None and _source_close_time(value) > scheduled:
                        raise ValueError(f"future_source_kline:{run_id}:{scheduled.isoformat()}")
                if scheduled in seen_within_run:
                    raise ValueError(f"duplicate_within_run:{run_id}:{scheduled.isoformat()}")
                seen_within_run.add(scheduled)
                grouped.setdefault(scheduled, []).append(row)
                events.append(scheduled)
        run_segments.append({
            "run_id": run_id, "status": run.get("status"),
            "started_at": run.get("started_at"), "ended_at": run.get("ended_at"),
            "first_event_at": min(events).isoformat() if events else None,
            "last_event_at": max(events).isoformat() if events else None,
            "event_count": len(events),
        })
        input_hashes.append({"run_id": run_id, "manifest_sha256": _sha256(manifest_path),
                             "events_sha256": _sha256(event_path)})

    retained: List[Dict[str, Any]] = []
    duplicate_count = 0
    duplicate_events: List[Dict[str, Any]] = []
    for scheduled, rows in sorted(grouped.items()):
        if len({_canonical_event(row) for row in rows}) != 1:
            raise ValueError(f"conflicting_duplicate:{scheduled.isoformat()}")
        rows.sort(key=lambda row: (_time(row["evaluated_at"]), row["run_id"]))
        retained.append(rows[0])
        duplicate_count += len(rows) - 1
        if len(rows) > 1:
            duplicate_events.append({
                "scheduled_at": scheduled.isoformat(),
                "chosen_run_id": rows[0]["run_id"],
                "source_run_ids": [row["run_id"] for row in rows],
            })
    missing = []
    cursor = start_at
    while cursor < end_at:
        if cursor not in grouped:
            missing.append(cursor.isoformat())
        cursor += timedelta(hours=1)
    degraded = sorted(source_missing_hours | {
        row["scheduled_at"] for row in retained if row.get("data_quality_reason")
    })
    summary = {
        "profile_id": PROFILE, "symbol": "XAUUSDT",
        "window_start": start_at.isoformat(), "window_end": end_at.isoformat(),
        "expected_hours": int((end_at - start_at).total_seconds() // 3600),
        "events_written": len(retained), "duplicates_removed": duplicate_count,
        "duplicate_events": duplicate_events,
        "missing_hours": missing, "quality_degraded_hours": degraded,
        "run_segments": sorted(run_segments, key=lambda item: item["run_id"]),
        "input_hashes": sorted(input_hashes, key=lambda item: item["run_id"]),
    }
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError(f"reconciliation output already exists: {output}")
    payload = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in retained)
    coverage_text = json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    identity = hashlib.sha256(json.dumps({
        "events": payload, "coverage": coverage_text, "config_hash": config_hash,
        "calibration_manifest_sha256": calibration["manifest_sha256"],
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    logical_manifest = {
        "run_id": "xau-calibrated-reconciled-" + identity,
        "instance_id": INSTANCE, "symbol": "XAUUSDT", "status": "reconciled",
        "initial_virtual_equity_usdt": expected_equity,
        "data_cutoff_at": end_at.isoformat(),
        "config_hashes": {PROFILE: config_hash},
        "source_runs": [item["run_id"] for item in summary["run_segments"]],
        "coverage_sha256": hashlib.sha256(coverage_text.encode()).hexdigest(),
        "decision_events_sha256": hashlib.sha256(payload.encode()).hexdigest(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-stage-", dir=output.parent) as temporary:
        stage = Path(temporary)
        (stage / "decision_events.jsonl").write_text(payload, encoding="utf-8")
        (stage / "coverage.json").write_text(coverage_text, encoding="utf-8")
        (stage / "run_manifest.json").write_text(
            json.dumps(logical_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if output.exists():
            raise FileExistsError(f"reconciliation output already exists: {output}")
        os.replace(stage, output)
    return summary


def main(argv: List[str] = None) -> int:
    """Local-only entry point; input exports must already exist on this machine."""
    import argparse

    parser = argparse.ArgumentParser(description="合并 XAU 校准版影子决策 run（离线只读输入）")
    parser.add_argument("--run-dir", action="append", required=True, help="一个物理 run 导出目录；可重复")
    parser.add_argument("--profile-config", required=True)
    parser.add_argument("--calibration-manifest", required=True)
    parser.add_argument("--calibration-snapshot", required=True)
    parser.add_argument("--start", required=True, help="含端点 UTC 小时")
    parser.add_argument("--end", required=True, help="不含端点 UTC 小时")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    summary = reconcile_run_exports(
        [Path(path) for path in args.run_dir], Path(args.profile_config),
        Path(args.calibration_manifest), Path(args.calibration_snapshot),
        args.start, args.end, Path(args.output_dir),
    )
    print(json.dumps({key: summary[key] for key in
                      ("events_written", "duplicates_removed", "missing_hours")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
