import pytest

from scripts.render_remote_deploy import RenderError, _validate
from scripts.scoped_strategy_successor import image_only, unchanged_exchange
from scripts.select_deploy_mode import DeployModeError, select_deploy_mode
from scripts.strategy_successor_manifest import (
    BASE, MANIFEST, MARKER, PIN_OVERLAY_SHA256, RELEASE, SOURCE,
    ReleaseBlocked, sha256, validate_manifest, validate_tree,
)


def _manifest():
    return {
        "schema_version": 1,
        "release_id": RELEASE,
        "base_revision": BASE,
        "expected_pin_overlay_sha256": PIN_OVERLAY_SHA256,
        "allowed_changes": [{"status": "A", "path": MARKER}, {"status": "A", "path": MANIFEST}],
        "source_checksums": {path: sha256(path.encode()) for path in SOURCE},
    }


def test_successor_requires_explicit_added_marker_and_exact_pin_scope():
    assert select_deploy_mode("push", BASE, [("A", MARKER)]) == "strategy-risk-successor"
    with pytest.raises(DeployModeError):
        select_deploy_mode("push", BASE, [("M", MARKER)])
    with pytest.raises(DeployModeError):
        select_deploy_mode("push", BASE, [("A", MARKER), ("A", ".github/deploy-modes/ledger-risk-remediation-20261104")])


def test_successor_tree_rejects_unreviewed_files_and_source_changes():
    manifest = _manifest()
    def read(path):
        return (MANIFEST if path == MARKER else path).encode()
    validate_tree(manifest, [("A", MARKER), ("A", MANIFEST)], read)
    with pytest.raises(ReleaseBlocked, match="outside reviewed"):
        validate_tree(manifest, [("A", MARKER), ("A", MANIFEST), ("M", "docker-compose.yml")], read)
    with pytest.raises(ReleaseBlocked, match="source checksum"):
        validate_tree(manifest, [("A", MARKER), ("A", MANIFEST)], lambda path: b"changed" if path in SOURCE else read(path))
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
