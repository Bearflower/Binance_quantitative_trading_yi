import pytest

from scripts.render_remote_deploy import RenderError, _validate
from scripts.risk_followup_manifest import (
    ACTIVE_PIN_SHA, BASE, IMAGES, MANIFEST, MARKER, PIN_OVERLAY_SHA256,
    RELEASE, SOURCE, ReleaseBlocked, sha256, validate_manifest, validate_tree,
)
from scripts.scoped_risk_followup import (
    Runtime, image_only, startup_marker_present, unchanged_exchange,
    validate_active_state,
)
from scripts.select_deploy_mode import DeployModeError, select_deploy_mode


def manifest():
    return {
        "schema_version": 1,
        "release_id": RELEASE,
        "base_revision": BASE,
        "active_pin_sha": ACTIVE_PIN_SHA,
        "expected_pin_overlay_sha256": PIN_OVERLAY_SHA256,
        "allowed_changes": [{"status": "A", "path": MARKER}, {"status": "A", "path": MANIFEST}],
        "source_checksums": {path: sha256(path.encode()) for path in SOURCE},
    }


def test_followup_selects_only_exact_added_marker_and_two_images():
    assert IMAGES == {"grid": "grid-strategy", "btc-eth-aggr": "btc-eth-aggressive-strategy"}
    assert select_deploy_mode("push", BASE, [("A", MARKER)]) == "strategy-risk-followup"
    for changes in [[("M", MARKER)], [("A", MARKER), ("A", ".github/deploy-modes/strategy-risk-successor-20261105")]]:
        with pytest.raises(DeployModeError):
            select_deploy_mode("push", BASE, changes)


def test_followup_manifest_rejects_scope_or_active_pin_drift():
    value = manifest()
    changes = [("A", MARKER), ("A", MANIFEST)]
    read = lambda path: (MANIFEST if path == MARKER else path).encode()
    validate_tree(value, changes, read)
    with pytest.raises(ReleaseBlocked, match="outside reviewed"):
        validate_tree(value, changes + [("M", "docker-compose.yml")], read)
    with pytest.raises(ReleaseBlocked, match="source checksum"):
        validate_tree(value, changes, lambda path: b"changed" if path in SOURCE else read(path))
    value["active_pin_sha"] = "0" * 40
    with pytest.raises(ReleaseBlocked, match="pin"):
        validate_manifest(value)


def test_followup_requires_completed_active_predecessor_and_exchange_proof():
    pin = {"sha": ACTIVE_PIN_SHA, "status": "active"}
    state = {"sha": ACTIVE_PIN_SHA, "status": "complete", "exchange_after": {"ok": True}}
    validate_active_state(pin, state)
    for changed_pin, changed_state in [
        (dict(pin, status="activating"), state),
        (pin, dict(state, status="failed")),
        (pin, dict(state, exchange_after={"ok": False})),
    ]:
        with pytest.raises(ReleaseBlocked):
            validate_active_state(changed_pin, changed_state)


def test_followup_accepts_gate_after_when_exchange_after_absent():
    """兼容人工后继脚本写入的 state.json：只有 gate_after 没有 exchange_after。"""
    pin = {"sha": ACTIVE_PIN_SHA, "status": "active"}
    # 只有 gate_after.ok=True 时通过
    validate_active_state(
        pin,
        {"sha": ACTIVE_PIN_SHA, "status": "complete", "gate_after": {"ok": True}},
    )
    # exchange_after 优先于 gate_after
    validate_active_state(
        pin,
        {"sha": ACTIVE_PIN_SHA, "status": "complete",
         "exchange_after": {"ok": True}, "gate_after": {"ok": False}},
    )


def test_followup_rejects_invalid_gate_after_forms():
    pin = {"sha": ACTIVE_PIN_SHA, "status": "active"}
    base_state = {"sha": ACTIVE_PIN_SHA, "status": "complete"}
    for bad_state in [
        dict(base_state, gate_after={"ok": False}),
        dict(base_state, gate_after={}),
        dict(base_state, gate_after=True),
        dict(base_state, gate_after="not-a-dict"),
        dict(base_state, exchange_after={"ok": False}, gate_after={"ok": False}),
    ]:
        with pytest.raises(ReleaseBlocked):
            validate_active_state(pin, bad_state)


def test_followup_preserves_config_exchange_inventory_and_startup_marker():
    before = {"image": "old", "environment": ["A=1"]}
    assert image_only(before, dict(before, image="new"))
    assert not image_only(before, dict(before, image="new", environment=["A=2"]))
    proof = {"position_exposure": [], "ordinary_orders_sha256": "a", "algo_orders_sha256": "b", "allocation_sha256": "c"}
    unchanged_exchange(proof, dict(proof))
    with pytest.raises(ReleaseBlocked):
        unchanged_exchange(proof, dict(proof, allocation_sha256="changed"))
    assert startup_marker_present('{"event": "\\u7b56\\u7565\\u521d\\u59cb\\u5316\\u5b8c\\u6210\\uff0c\\u5f00\\u59cb\\u8fd0\\u884c"}', "策略初始化完成，开始运行")


def test_followup_never_rotates_funding_credentials():
    _validate("strategy-risk-followup", "a" * 40, "12345678", "keep")
    with pytest.raises(RenderError):
        _validate("strategy-risk-followup", "a" * 40, "12345678", "rotate")


def test_expired_policy_release_uses_read_only_pre_activation_gate():
    runtime = Runtime.__new__(Runtime)
    calls = []
    runtime.run = lambda argv, **kwargs: calls.append(argv) or '{"ok":true,"read_only":true}'
    assert runtime.gate()["ok"] is True
    assert calls[0][calls[0].index("--stage") + 1] == "pre_activation"
