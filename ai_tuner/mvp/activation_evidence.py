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
