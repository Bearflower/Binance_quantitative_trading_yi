"""Fixed scope for the STOP evidence migration and aggressive-only release."""
from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

RELEASE = "aggressive-stop-followup-20261109"
MODE = "strategy-stop-followup"
MARKER = f".github/deploy-modes/{RELEASE}"
MANIFEST = f"deployment/{RELEASE}.json"
BASE = "7da9a4983cd626dd6b2bfe9a9c3f2a70df706aa1"
ACTIVE_PIN_SHA = "6279ded829c8013062d24b3841f912d1b340fe9c"
PIN_OVERLAY_SHA256 = "bd568038225f492c4b744e3da516db9d1ee293ef4a85a8e06774d68200536ccf"
IMAGES = {
    "btc-eth-aggr": "btc-eth-aggressive-strategy",
}
SOURCE = {
    ".github/workflows/deploy.yml",
    "scripts/select_deploy_mode.py",
    "scripts/render_remote_deploy.py",
    "scripts/remote_deploy_runtime.sh",
    "scripts/stop_followup_manifest.py",
    "scripts/package_stop_followup.py",
    "scripts/scoped_stop_followup.py",
    "database/postgres/init-scripts/16-aggressive-stop-bindings.sql",
    "shared/binance_api.py",
    "shared/accounting/stop_exit_evidence.py",
    "shared/accounting/stop_binding.py",
    "shared/accounting/stop_episode_evidence.py",
    "shared/accounting/stop_exit_audit.py",
    "shared/accounting/stop_exit_ledger.py",
    "shared/accounting/stop_intent_journal.py",
    "shared/accounting/stop_intent_recovery.py",
    "shared/entry_risk_policy.py",
    "shared/protection_retry.py",
    "strategies/btc_eth_aggressive/main.py",
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
