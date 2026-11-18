from dataclasses import replace

import pytest

from ai_tuner.mvp.activation_evidence import (
    ActivationIdentity,
    RuntimeReceipt,
    evaluate_receipt,
    require_qualified_activation,
)
from ai_tuner.qualification.gates import GATE_SPECS


@pytest.fixture
def identity():
    return ActivationIdentity(
        strategy_id="btc_eth_aggressive", version="V2", baseline_version="V1",
        overlay_hash="a" * 64, effective_params_hash="b" * 64, evidence_hash="c" * 64,
        policy_hash="d" * 64, manifest_schema_version="1", strategy_revision="commit-1",
    )


@pytest.fixture
def facts():
    return {
        "status": "PASS", "source_provenance": "verified",
        "gates": {spec[0]: {"pass": True} for spec in GATE_SPECS},
    }


def test_activation_binds_verified_gates_to_exact_identity(identity, facts):
    require_qualified_activation(identity, identity, facts)
    with pytest.raises(ValueError, match="identity"):
        require_qualified_activation(replace(identity, baseline_version="V0"), identity, facts)


@pytest.mark.parametrize("mutation", ["blocked", "synthetic", "missing_gate", "failed_gate"])
def test_activation_rejects_incomplete_qualification(identity, facts, mutation):
    if mutation == "blocked":
        facts["status"] = "BLOCKED_DATA"
    elif mutation == "synthetic":
        facts["source_provenance"] = "synthetic"
    elif mutation == "missing_gate":
        del facts["gates"]["golden_parity"]
    else:
        facts["gates"]["golden_parity"]["pass"] = False
    with pytest.raises(ValueError, match="qualification"):
        require_qualified_activation(identity, identity, facts)


def test_activation_acknowledgement_must_match_current_attempt(identity):
    good = RuntimeReceipt("request-2", identity.strategy_id, "V2", "V2", "b" * 64, "1")
    assert evaluate_receipt(identity, "request-2", good) == "verified"
    assert evaluate_receipt(identity, "request-2", None) == "pending"
    assert evaluate_receipt(identity, "request-2", None, expired=True) == "restore_previous"
    assert evaluate_receipt(identity, "request-2", replace(good, request_id="old")) == "pending"
    assert evaluate_receipt(identity, "request-2", replace(good, loaded_version="V1")) == "restore_previous"
    assert evaluate_receipt(identity, "request-2", replace(good, effective_params_hash="e" * 64)) == "restore_previous"
    assert evaluate_receipt(identity, "request-2", replace(good, manifest_schema_version="0")) == "restore_previous"
    assert evaluate_receipt(identity, "request-2", good, expired=True) == "restore_previous"
