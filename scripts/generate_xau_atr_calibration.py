#!/usr/bin/env python3
"""Build and freeze deterministic XAU ATR calibration evidence locally."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.btc_eth.shadow.atr_calibration import (  # noqa: E402
    CalibrationError,
    build_calibration,
    canonical_snapshot_bytes,
    verify_manifest,
)


class GeneratorError(ValueError):
    """Raised when CLI input or output safety checks fail."""


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate immutable XAUUSDT ATR calibration evidence"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--snapshot-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--profile-config", type=Path, required=True)
    parser.add_argument("--cutoff-ms", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="allow replacing different snapshot and manifest outputs",
    )
    return parser.parse_args(argv)


def _load_rows(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GeneratorError(f"input_read_failed:{path}") from exc
    if not payload.strip():
        raise GeneratorError("input_empty")

    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        rows: List[Dict[str, Any]] = []
        for line_number, line in enumerate(payload.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise GeneratorError(f"invalid_jsonl_line:{line_number}") from exc
            if not isinstance(row, dict):
                raise GeneratorError(f"jsonl_row_not_object:{line_number}")
            rows.append(row)
        if not rows:
            raise GeneratorError("input_empty")
        return rows

    if isinstance(decoded, list):
        if not decoded:
            raise GeneratorError("input_empty")
        if not all(isinstance(row, dict) for row in decoded):
            raise GeneratorError("json_array_rows_must_be_objects")
        return decoded
    if isinstance(decoded, dict):
        return [decoded]
    raise GeneratorError("input_must_be_json_array_or_jsonl_objects")


def _resolve_cutoff(raw: str, rows: Iterable[Mapping[str, Any]]) -> int:
    if raw != "max":
        try:
            cutoff = int(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise GeneratorError("cutoff_ms_must_be_integer_or_max") from exc
        if cutoff <= 0:
            raise GeneratorError("cutoff_ms_must_be_positive")
        return cutoff

    close_times: List[int] = []
    for row in rows:
        try:
            value = int(row["close_time"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise GeneratorError("max_cutoff_requires_integer_close_time") from exc
        close_times.append(value)
    if not close_times:
        raise GeneratorError("max_cutoff_requires_rows")
    return max(close_times)


def _resolve_git_commit(raw: str) -> str:
    if raw != "auto":
        if not raw.strip():
            raise GeneratorError("git_commit_empty")
        return raw
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GeneratorError("git_commit_auto_failed") from exc
    commit = completed.stdout.strip()
    if not commit:
        raise GeneratorError("git_commit_auto_empty")
    return commit


def _updated_profile_bytes(
    profile_path: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
) -> bytes:
    try:
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GeneratorError(f"profile_read_failed:{profile_path}") from exc
    try:
        symbol_config = profile["strategy"]["symbol_config"]["XAUUSDT"]
        shadow = profile["shadow"]
        calibration = manifest["calibration"]
    except (KeyError, TypeError) as exc:
        raise GeneratorError("profile_missing_calibration_paths") from exc
    if not isinstance(symbol_config, dict) or not isinstance(shadow, dict):
        raise GeneratorError("profile_calibration_paths_must_be_mappings")
    calibration_manifest = shadow.get("calibration_manifest")
    if not isinstance(calibration_manifest, str) or not calibration_manifest:
        raise GeneratorError("profile_calibration_manifest_missing")
    if Path(calibration_manifest).is_absolute():
        raise GeneratorError("profile_calibration_manifest_must_be_relative")
    configured_manifest_path = (ROOT / calibration_manifest).resolve()
    if configured_manifest_path != manifest_path.resolve():
        raise GeneratorError("manifest_output_does_not_match_profile")

    symbol_config["atr_abs_min"] = calibration["atr_abs_min"]
    symbol_config["atr_fallback_percent"] = calibration["fallback_atr_percent"]
    shadow["calibration_manifest_sha256"] = manifest["manifest_sha256"]
    shadow["calibration_snapshot_sha256"] = manifest["snapshot_sha256"]
    shadow["calibration_cutoff_ms"] = manifest["cutoff_ms"]
    try:
        return yaml.safe_dump(
            profile,
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
    except yaml.YAMLError as exc:
        raise GeneratorError("profile_not_serializable") from exc


def _preflight_output(path: Path, content: bytes, *, replace: bool) -> None:
    if not path.exists():
        return
    try:
        existing = path.read_bytes()
    except OSError as exc:
        raise GeneratorError(f"output_read_failed:{path}") from exc
    if existing != content and not replace:
        raise GeneratorError(f"output_exists_with_different_content:{path}")


def _stage(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with staged.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _restore(path: Path, original: Optional[bytes]) -> None:
    if original is None:
        path.unlink(missing_ok=True)
        return
    restored = _stage(path, original)
    os.replace(restored, path)


def _commit_transaction(outputs: Sequence[Tuple[Path, bytes]]) -> None:
    resolved = [path.resolve() for path, _content in outputs]
    if len(resolved) != len(set(resolved)):
        raise GeneratorError("output_paths_must_be_distinct")

    originals: Dict[Path, Optional[bytes]] = {}
    staged: Dict[Path, Path] = {}
    changed: List[Path] = []
    try:
        for path, content in outputs:
            originals[path] = path.read_bytes() if path.exists() else None
            if originals[path] == content:
                continue
            staged[path] = _stage(path, content)
        for path, _content in outputs:
            temp = staged.get(path)
            if temp is None:
                continue
            os.replace(temp, path)
            changed.append(path)
    except BaseException:
        for temp in staged.values():
            temp.unlink(missing_ok=True)
        rollback_errors = []
        for path in reversed(changed):
            try:
                _restore(path, originals[path])
            except BaseException as rollback_exc:  # best-effort transaction rollback
                rollback_errors.append(f"{path}:{rollback_exc}")
        if rollback_errors:
            raise GeneratorError(
                "output_transaction_rollback_failed:" + ";".join(rollback_errors)
            )
        raise


def _validate_path_topology(args: argparse.Namespace) -> None:
    input_path = args.input.resolve()
    output_paths = [
        args.snapshot_output.resolve(),
        args.manifest_output.resolve(),
        args.profile_config.resolve(),
    ]
    if input_path in output_paths:
        raise GeneratorError("input_path_must_not_be_an_output")
    if len(output_paths) != len(set(output_paths)):
        raise GeneratorError("output_paths_must_be_distinct")


def _run(args: argparse.Namespace) -> Dict[str, Any]:
    _validate_path_topology(args)
    rows = _load_rows(args.input)
    cutoff_ms = _resolve_cutoff(args.cutoff_ms, rows)
    git_commit = _resolve_git_commit(args.git_commit)
    result = build_calibration(rows, cutoff_ms=cutoff_ms)
    snapshot_bytes = canonical_snapshot_bytes(result.snapshot_rows)
    generated_at = datetime.now(timezone.utc).isoformat()
    manifest = result.to_manifest(
        source=args.source,
        git_commit=git_commit,
        snapshot_sha256=result.snapshot_sha256,
        generated_at=generated_at,
    )
    verify_manifest(manifest, snapshot_bytes)
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    profile_bytes = _updated_profile_bytes(
        args.profile_config,
        args.manifest_output,
        manifest,
    )

    _preflight_output(
        args.snapshot_output,
        snapshot_bytes,
        replace=args.replace,
    )
    _preflight_output(
        args.manifest_output,
        manifest_bytes,
        replace=args.replace,
    )
    _commit_transaction(
        (
            (args.snapshot_output, snapshot_bytes),
            (args.manifest_output, manifest_bytes),
            (args.profile_config, profile_bytes),
        )
    )
    return {
        "generated_at": generated_at,
        "snapshot_sha256": result.snapshot_sha256,
        "manifest_sha256": manifest["manifest_sha256"],
        "valid_rows": result.valid_rows,
        "atr_sample_count": result.atr_sample_count,
        "atr_abs_min": result.atr_abs_min,
        "fallback_atr_percent": result.p50_percent,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        summary = _run(args)
    except (CalibrationError, GeneratorError, OSError, ValueError) as exc:
        print(f"calibration_generation_failed:{exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            summary,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
