"""Immutable scope and source validation for the 2026-11-05 strategy successor."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

RELEASE = "strategy-risk-successor-20261105"
MODE = "strategy-risk-successor"
MARKER = f".github/deploy-modes/{RELEASE}"
RECOVERY_MARKER = f".github/deploy-modes/{RELEASE}-recovery-1"
MANIFEST = f"deployment/{RELEASE}.json"
BASE = "d77c232881d75f65ae13ee4fcad33946b6c8413a"
PIN_OVERLAY_SHA256 = "2240ce1371ba46cc9999497a480d8d09ebb9fa85ec684dfb97aec41ceb865abe"
IMAGES = {"btc-eth-aggr": "btc-eth-aggressive-strategy", "new-coin": "new-coin-strategy"}
SOURCE = {
    ".github/workflows/deploy.yml",
    "shared/entry_risk_policy.py",
    "strategies/btc_eth_aggressive/strategy.py",
    "strategies/new_coin/executor.py",
    "strategies/new_coin/strategy.py",
    "scripts/scoped_strategy_successor.py",
    "scripts/strategy_successor_manifest.py",
    "scripts/package_strategy_successor.py",
    "scripts/remote_deploy_runtime.sh",
    "scripts/render_remote_deploy.py",
    "scripts/select_deploy_mode.py",
}
SHA = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseBlocked(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_manifest(manifest: dict) -> dict:
    if set(manifest) != {"schema_version", "release_id", "base_revision", "expected_pin_overlay_sha256", "allowed_changes", "source_checksums"}:
        raise ReleaseBlocked("invalid successor manifest fields")
    if manifest["schema_version"] != 1 or manifest["release_id"] != RELEASE or manifest["base_revision"] != BASE:
        raise ReleaseBlocked("unexpected release identity or baseline")
    if manifest["expected_pin_overlay_sha256"] != PIN_OVERLAY_SHA256:
        raise ReleaseBlocked("unreviewed active pin overlay")
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
        if not isinstance(path, str) or not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts or "\\" in path:
            raise ReleaseBlocked("unsafe changed path")
    return manifest


def validate_tree(manifest: dict, changes: list[tuple[str, str]], read) -> None:
    validate_manifest(manifest)
    expected = sorted((row["status"], row["path"]) for row in manifest["allowed_changes"])
    if sorted(changes) != expected:
        raise ReleaseBlocked("push contains changes outside reviewed successor scope")
    for marker in (MARKER, RECOVERY_MARKER):
        if ("A", marker) not in changes or read(marker).decode().strip() != MANIFEST:
            raise ReleaseBlocked("successor marker missing or invalid: " + marker)
    if ("A", MANIFEST) not in changes:
        raise ReleaseBlocked("successor manifest must be added in this push")
    for path, expected_hash in manifest["source_checksums"].items():
        if sha256(read(path)) != expected_hash:
            raise ReleaseBlocked("source checksum mismatch: " + path)
