import pytest

from scripts.render_remote_deploy import RenderError, _validate
from scripts.scoped_strategy_successor import (
    PARTIAL_AGGR, PARTIAL_NEW_COIN, PARTIAL_SHA, image_only,
    startup_marker_present, unchanged_exchange, validate_partial_state,
)
from scripts.select_deploy_mode import DeployModeError, select_deploy_mode
from scripts.strategy_successor_manifest import (
    BASE, MANIFEST, MARKER, RECOVERY_MARKER, PIN_OVERLAY_SHA256, RELEASE, SOURCE,
    ReleaseBlocked, sha256, validate_manifest, validate_tree,
)


def _manifest():
    return {
        "schema_version": 1,
        "release_id": RELEASE,
        "base_revision": BASE,
        "expected_pin_overlay_sha256": PIN_OVERLAY_SHA256,
        "allowed_changes": [{"status": "A", "path": MARKER}, {"status": "A", "path": RECOVERY_MARKER}, {"status": "A", "path": MANIFEST}],
        "source_checksums": {path: sha256(path.encode()) for path in SOURCE},
    }


def test_successor_requires_explicit_added_marker_and_exact_pin_scope():
    assert select_deploy_mode("push", BASE, [("A", MARKER)]) == "strategy-risk-successor"
    assert select_deploy_mode("push", "9d6c6c61da1e42ea378095ce6065ba463b2fb686", [("A", RECOVERY_MARKER)]) == "strategy-risk-successor"
    with pytest.raises(DeployModeError):
        select_deploy_mode("push", BASE, [("M", MARKER)])
    with pytest.raises(DeployModeError):
        select_deploy_mode("push", BASE, [("A", MARKER), ("A", ".github/deploy-modes/ledger-risk-remediation-20261104")])


def test_successor_tree_rejects_unreviewed_files_and_source_changes():
    manifest = _manifest()
    def read(path):
        return (MANIFEST if path in (MARKER, RECOVERY_MARKER) else path).encode()
    changes = [("A", MARKER), ("A", RECOVERY_MARKER), ("A", MANIFEST)]
    validate_tree(manifest, changes, read)
    with pytest.raises(ReleaseBlocked, match="outside reviewed"):
        validate_tree(manifest, changes + [("M", "docker-compose.yml")], read)
    with pytest.raises(ReleaseBlocked, match="source checksum"):
        validate_tree(manifest, changes, lambda path: b"changed" if path in SOURCE else read(path))
    manifest["expected_pin_overlay_sha256"] = "0" * 64
    with pytest.raises(ReleaseBlocked, match="overlay"):
        validate_manifest(manifest)


def test_successor_only_changes_images_and_preserves_exchange_inventory():
    old = {"image": "old", "environment": ["A=1"], "volumes": ["data:/app/data"]}
    assert image_only(old, dict(old, image="new"))
    assert not image_only(old, dict(old, image="new", environment=["A=2"]))
    before = {"position_exposure": [{"symbol": "PFEUSDT", "positionAmt": "-3.28"}],
              "ordinary_orders_sha256": "a", "algo_orders_sha256": "b", "allocation_sha256": "c"}
    unchanged_exchange(before, dict(before))
    for key in before:
        with pytest.raises(ReleaseBlocked, match="manual review"):
            unchanged_exchange(before, dict(before, **{key: "changed"}))


def test_successor_cannot_rotate_funding_credentials():
    _validate("strategy-risk-successor", "a" * 40, "12345678", "keep")
    with pytest.raises(RenderError):
        _validate("strategy-risk-successor", "a" * 40, "12345678", "rotate")


def test_recovery_requires_exact_failed_single_service_state():
    pin = {"sha": BASE, "status": "activating", "successor_sha": PARTIAL_SHA}
    state = {"sha": PARTIAL_SHA, "status": "failed", "step": "ReleaseBlocked",
             "changed": ["btc-eth-aggressive-strategy"],
             "images": {"btc-eth-aggr": PARTIAL_AGGR, "new-coin": PARTIAL_NEW_COIN}}
    validate_partial_state(pin, state)
    with pytest.raises(ReleaseBlocked):
        validate_partial_state(dict(pin, status="active"), state)
    with pytest.raises(ReleaseBlocked):
        validate_partial_state(pin, dict(state, changed=["new-coin-strategy"]))


def test_startup_marker_accepts_structured_json_unicode_escaping():
    marker = "调度器已启动"
    assert startup_marker_present('{"event": "\\u8c03\\u5ea6\\u5668\\u5df2\\u542f\\u52a8"}', marker)
    assert startup_marker_present(marker, marker)
    assert not startup_marker_present("no startup event", marker)
