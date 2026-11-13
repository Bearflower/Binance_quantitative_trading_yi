"""One-shot AI Tuner trial observer release identity and exact push scope."""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
import re

RELEASE = "xau-ai-tuner-patrol-20261113"
MODE = "ai-tuner-trial"
MARKER = f".github/deploy-modes/{RELEASE}"
MANIFEST = f"deployment/{RELEASE}.json"
BASE = "7a320a59daacbc960c0b732abc417b1025082d3d"
ACTIVE_PIN_SHA = "4a9d8ce7f923456ceb4dc7639b70314bf2ceed86"
PIN_OVERLAY_SHA256 = "1f4f5abea149c14324278286ecbdfbb691e14e6964c8eabe7c2495c5a8781da7"
IMAGES = {"ai-tuner": "ai-tuner"}
SOURCE = {
    ".github/workflows/deploy.yml",
    "scripts/select_deploy_mode.py",
    "scripts/render_remote_deploy.py",
    "scripts/remote_deploy_runtime.sh",
    "scripts/ai_tuner_trial_manifest.py",
    "scripts/package_ai_tuner_trial.py",
    "scripts/scoped_ai_tuner_trial.py",
    "scripts/scoped_stop_followup.py",
    "scripts/stop_followup_manifest.py",
    "ai_tuner/config.yaml",
    "ai_tuner/main.py",
    "ai_tuner/monitor/daily_health_check.py",
    "ai_tuner/monitor/reduced_trial.py",
    "ai_tuner/monitor/xau_shadow_patrol.py",
    "shared/accounting/report_reader.py",
    "shared/readonly_client.py",
}
SHA = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseBlocked(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_manifest(manifest: dict) -> dict:
    if set(manifest) != {"schema_version", "release_id", "base_revision", "active_pin_sha",
                         "expected_pin_overlay_sha256", "allowed_changes", "source_checksums"}:
        raise ReleaseBlocked("invalid AI Tuner manifest fields")
    if (manifest["schema_version"], manifest["release_id"], manifest["base_revision"],
            manifest["active_pin_sha"], manifest["expected_pin_overlay_sha256"]) != (
            1, RELEASE, BASE, ACTIVE_PIN_SHA, PIN_OVERLAY_SHA256):
        raise ReleaseBlocked("unexpected AI Tuner release identity")
    checksums = manifest["source_checksums"]
    if set(checksums) != SOURCE or any(not HEX64.fullmatch(value) for value in checksums.values()):
        raise ReleaseBlocked("required source checksums missing")
    changes = manifest["allowed_changes"]
    if not isinstance(changes, list) or len(changes) != len({(row.get("status"), row.get("path")) for row in changes}):
        raise ReleaseBlocked("ambiguous changed paths")
    for row in changes:
        if set(row) != {"status", "path"} or row["status"] not in {"A", "M", "D"}:
            raise ReleaseBlocked("invalid change record")
        path = row["path"]
        if (not isinstance(path, str) or not path or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts or "\\" in path):
            raise ReleaseBlocked("unsafe changed path")
    return manifest


def validate_tree(manifest: dict, changes: list[tuple[str, str]], read) -> None:
    validate_manifest(manifest)
    if sorted(changes) != sorted((row["status"], row["path"]) for row in manifest["allowed_changes"]):
        raise ReleaseBlocked("push contains changes outside reviewed AI Tuner scope")
    if ("A", MARKER) not in changes or read(MARKER).decode().strip() != MANIFEST:
        raise ReleaseBlocked("AI Tuner release marker missing or invalid")
    if ("A", MANIFEST) not in changes:
        raise ReleaseBlocked("AI Tuner manifest must be added")
    for path, expected_hash in manifest["source_checksums"].items():
        if sha256(read(path)) != expected_hash:
            raise ReleaseBlocked("source checksum mismatch: " + path)
