"""Fixed scope for the 2026-11-06 successor to the active image pin."""
from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

RELEASE = "strategy-risk-followup-20261106"
MODE = "strategy-risk-followup"
MARKER = f".github/deploy-modes/{RELEASE}"
MANIFEST = f"deployment/{RELEASE}.json"
BASE = "4ca5230a591f52742d737ac82f12947db4fd809c"
ACTIVE_PIN_SHA = "4ca5230a591f52742d737ac82f12947db4fd809c"
PIN_OVERLAY_SHA256 = "d8be15cda5f632c8c755e8fd6954ab96f62423bcdb5676bc777594f3d39327a1"
IMAGES = {
    "hrs": "hrs-strategy",
    "btc-eth-aggr": "btc-eth-aggressive-strategy",
}
SOURCE = {
    ".github/workflows/deploy.yml",
    "scripts/select_deploy_mode.py",
    "scripts/render_remote_deploy.py",
    "scripts/remote_deploy_runtime.sh",
    "scripts/risk_followup_manifest.py",
    "scripts/package_risk_followup.py",
    "scripts/scoped_risk_followup.py",
    "scripts/verify_ledger_risk_release.py",
    "shared/entry_risk_policy.py",
    "strategies/btc_eth_aggressive/strategy.py",
}
SHA = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseBlocked(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_manifest(manifest: dict) -> dict:
    if set(manifest) != {"schema_version", "release_id", "base_revision", "active_pin_sha", "expected_pin_overlay_sha256", "allowed_changes", "source_checksums"}:
        raise ReleaseBlocked("invalid followup manifest fields")
    if (manifest["schema_version"], manifest["release_id"], manifest["base_revision"],
            manifest["active_pin_sha"], manifest["expected_pin_overlay_sha256"]) != (
            1, RELEASE, BASE, ACTIVE_PIN_SHA, PIN_OVERLAY_SHA256):
        raise ReleaseBlocked("unexpected followup identity or production pin")
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
    if sorted(changes) != sorted((row["status"], row["path"]) for row in manifest["allowed_changes"]):
        raise ReleaseBlocked("push contains changes outside reviewed followup scope")
    if ("A", MARKER) not in changes or read(MARKER).decode().strip() != MANIFEST:
        raise ReleaseBlocked("followup marker missing or invalid")
    if ("A", MANIFEST) not in changes:
        raise ReleaseBlocked("followup manifest must be added")
    for path, expected_hash in manifest["source_checksums"].items():
        if sha256(read(path)) != expected_hash:
            raise ReleaseBlocked("source checksum mismatch: " + path)
