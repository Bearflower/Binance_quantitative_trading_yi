"""Offline checks for qualification identity and runtime acknowledgement.

Reports must come from a trusted qualification store. These checks do not
authenticate that store, change an active pointer, or request a runtime reload.
"""

from dataclasses import dataclass
import re
from typing import Literal, Mapping

from ai_tuner.qualification.gates import GATE_SPECS


@dataclass(frozen=True)
class ActivationIdentity:
    strategy_id: str
    version: str
    baseline_version: str
    overlay_hash: str
    effective_params_hash: str
    evidence_hash: str
    policy_hash: str
    manifest_schema_version: str
    strategy_revision: str


@dataclass(frozen=True)
class RuntimeReceipt:
    request_id: str
    strategy_id: str
    requested_version: str
    loaded_version: str
    effective_params_hash: str
    manifest_schema_version: str


def require_qualified_activation(
    requested: ActivationIdentity, qualified: ActivationIdentity, report: Mapping[str, object],
) -> None:
    """Reject a request unless its exact identity has all verified qualification gates."""
    if requested != qualified:
        raise ValueError("qualification identity mismatch")
    labels = (
        requested.strategy_id, requested.version, requested.baseline_version,
        requested.manifest_schema_version, requested.strategy_revision,
    )
    hashes = (
        requested.overlay_hash, requested.effective_params_hash,
        requested.evidence_hash, requested.policy_hash,
    )
    if not all(isinstance(label, str) and label.strip() for label in labels) or not all(
        isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes
    ):
        raise ValueError("invalid qualification identity")
    gates = report.get("gates")
    if report.get("status") != "PASS" or report.get("source_provenance") != "verified":
        raise ValueError("qualification is not a verified PASS")
    if not isinstance(gates, Mapping) or not all(
        isinstance(gates.get(name), Mapping) and gates[name].get("pass") is True
        for name, *_ in GATE_SPECS
    ):
        raise ValueError("qualification gates are incomplete or failed")


def evaluate_receipt(
    requested: ActivationIdentity, request_id: str, receipt: RuntimeReceipt | None, *, expired: bool = False,
) -> Literal["pending", "verified", "restore_previous"]:
    """Adjudicate the current attempt. Its caller owns deadlines and CAS rollback."""
    if not request_id or not request_id.strip():
        raise ValueError("empty activation request id")
    if expired:
        return "restore_previous"
    if receipt is None or receipt.request_id != request_id:
        return "pending"
    expected = RuntimeReceipt(
        request_id, requested.strategy_id, requested.version, requested.version,
        requested.effective_params_hash, requested.manifest_schema_version,
    )
    return "verified" if receipt == expected else "restore_previous"


def validate_cycle_reload(
    current: Mapping[str, object], candidate: Mapping[str, object], *,
    allowed_paths: set[str], expected_hash: str,
) -> tuple[str, ...]:
    """Validate an entire loaded config before applying it at a cycle boundary."""
    from shared.config_loader import compute_config_hash

    if compute_config_hash(dict(candidate)) != expected_hash:
        raise ValueError("candidate effective config hash mismatch")
    changed = []

    def visit(before, after, prefix=""):
        if set(before) - set(after):
            raise ValueError(f"configuration keys removed at {prefix or 'root'}")
        for key, value in after.items():
            if not isinstance(key, str) or not key or "." in key:
                raise ValueError("invalid configuration path")
            path = f"{prefix}.{key}" if prefix else key
            if key not in before:
                raise ValueError(f"new runtime configuration key: {path}")
            former = before[key]
            if isinstance(former, Mapping) and isinstance(value, Mapping):
                visit(former, value, path)
            elif type(former) is not type(value) or former != value:
                if path not in allowed_paths or type(former) is not type(value):
                    raise ValueError(f"configuration change denied: {path}")
                changed.append(path)

    visit(current, candidate)
    return tuple(sorted(changed))


def evaluate_recovery_receipt(
    requested: ActivationIdentity, receipt: RuntimeReceipt | None, *,
    activation_request_id: str, recovery_request_id: str, baseline_effective_hash: str,
    expired: bool = False,
) -> Literal["pending", "restored", "recovery_unverified"]:
    """Require an independent confirmation of the baseline after pointer recovery."""
    if (not activation_request_id.strip() or not recovery_request_id.strip()
            or activation_request_id == recovery_request_id):
        raise ValueError("activation and recovery require distinct request ids")
    if not re.fullmatch(r"[0-9a-f]{64}", baseline_effective_hash):
        raise ValueError("invalid baseline config hash")
    if expired:
        return "recovery_unverified"
    if receipt is None or receipt.request_id != recovery_request_id:
        return "pending"
    expected = RuntimeReceipt(
        recovery_request_id, requested.strategy_id, requested.baseline_version,
        requested.baseline_version, baseline_effective_hash, requested.manifest_schema_version,
    )
    return "restored" if receipt == expected else "recovery_unverified"
