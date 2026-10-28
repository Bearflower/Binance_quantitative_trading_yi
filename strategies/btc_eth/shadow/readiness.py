"""影子采集器运行态就绪标记与只读探针。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Mapping


DEFAULT_READINESS_FILE = Path("/tmp/mtcps-xau-shadow-ready.json")


def readiness_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.getenv("SHADOW_READINESS_FILE", str(DEFAULT_READINESS_FILE)))


def clear_readiness(*, path: str | Path | None = None) -> None:
    readiness_path(path).unlink(missing_ok=True)


def write_readiness(
    payload: Mapping[str, object],
    *,
    path: str | Path | None = None,
) -> None:
    target = readiness_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(dict(payload), stream, sort_keys=True, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _expected_contract(environ: Mapping[str, str]) -> dict[str, object]:
    profiles = [
        profile.strip()
        for profile in environ.get("SHADOW_PROFILES", "").split(",")
        if profile.strip()
    ]
    return {
        "instance_id": environ.get("INSTANCE_ID", "mtcps_xau_shadow"),
        "profiles": profiles or ["xau_original", "xau_aggressive"],
        "run_id_prefix": environ.get("SHADOW_RUN_ID_PREFIX", "xau-shadow"),
        "cron": environ.get("SHADOW_CRON", "15 * * * *"),
        "execution_mode": environ.get("EXECUTION_MODE", "shadow_batch"),
        "real_ordering_enabled": False,
        "state_reporting_enabled": False,
    }


def _pid_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return False
    return True


def check_readiness(
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    environment = os.environ if environ is None else environ
    for safety_key in ("REAL_ORDERING_ENABLED", "STATE_REPORTING_ENABLED"):
        if environment.get(safety_key, "false").strip().lower() != "false":
            return False, f"unsafe environment: {safety_key}"

    target = readiness_path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"readiness marker unavailable: {exc}"
    if not isinstance(payload, dict):
        return False, "readiness marker must be an object"
    if not _pid_is_alive(payload.get("pid")):
        return False, "pid is not alive"

    expected = _expected_contract(environment)
    for field, expected_value in expected.items():
        if payload.get(field) != expected_value:
            return False, f"{field} mismatch"

    run_id = payload.get("run_id")
    prefix = expected["run_id_prefix"]
    if not isinstance(run_id, str) or not run_id.startswith(f"{prefix}-"):
        return False, "run_id mismatch"
    return True, "ready"


def main() -> int:
    ok, reason = check_readiness()
    if not ok:
        print(reason, file=sys.stderr)
        return 1
    print(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
