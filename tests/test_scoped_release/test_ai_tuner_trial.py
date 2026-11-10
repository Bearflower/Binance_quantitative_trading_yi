from pathlib import Path

import pytest

from scripts.ai_tuner_trial_manifest import (
    ACTIVE_PIN_SHA, BASE, IMAGES, MANIFEST, MARKER, PIN_OVERLAY_SHA256,
    RELEASE, SOURCE, ReleaseBlocked, sha256, validate_manifest, validate_tree,
)
from scripts.render_remote_deploy import RenderError, _validate
from scripts.scoped_ai_tuner_trial import Runtime, INCOME_RELEASE_SHA, validated_dashboard_image
from scripts.select_deploy_mode import DeployModeError, select_deploy_mode


def manifest():
    return {
        "schema_version": 1, "release_id": RELEASE, "base_revision": BASE,
        "active_pin_sha": ACTIVE_PIN_SHA,
        "expected_pin_overlay_sha256": PIN_OVERLAY_SHA256,
        "allowed_changes": [{"status": "A", "path": MARKER},
                            {"status": "A", "path": MANIFEST}],
        "source_checksums": {path: sha256(path.encode()) for path in SOURCE},
    }


def test_mode_is_one_image_and_requires_one_new_marker():
    assert IMAGES == {"ai-tuner": "ai-tuner"}
    assert select_deploy_mode("push", BASE, [("A", MARKER)]) == "ai-tuner-trial"
    for changes in ([("M", MARKER)], [("A", MARKER), ("A", ".github/deploy-modes/aggressive-stop-followup-20261109")]):
        with pytest.raises(DeployModeError):
            select_deploy_mode("push", BASE, changes)


def test_manifest_rejects_extra_path_bad_pin_and_wrong_source():
    value = manifest()
    changes = [("A", MARKER), ("A", MANIFEST)]
    read = lambda path: (MANIFEST if path == MARKER else path).encode()
    validate_tree(value, changes, read)
    with pytest.raises(ReleaseBlocked, match="outside reviewed"):
        validate_tree(value, changes + [("M", "docker-compose.yml")], read)
    value["source_checksums"]["ai_tuner/main.py"] = "0" * 64
    with pytest.raises(ReleaseBlocked, match="source checksum"):
        validate_tree(value, changes, read)
    value = manifest()
    value["active_pin_sha"] = "0" * 40
    with pytest.raises(ReleaseBlocked):
        validate_manifest(value)


def test_funding_rotation_is_forbidden():
    _validate("ai-tuner-trial", "a" * 40, "12345678", "keep")
    with pytest.raises(RenderError):
        _validate("ai-tuner-trial", "a" * 40, "12345678", "rotate")


def test_preflight_rejects_pin_drift_before_creating_release_directory(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    runtime.pin_file = tmp_path / "pin.json"
    runtime.release_dir = tmp_path / "release"
    runtime.pin_file.write_text('{"sha":"other","status":"active"}')
    with pytest.raises(ReleaseBlocked, match="pin changed"):
        runtime.preflight()
    assert not runtime.release_dir.exists()


def test_preflight_rejects_incomplete_predecessor_before_exchange_gate(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    runtime.pin_file = tmp_path / "pin.json"
    runtime.release_dir = tmp_path / "release"
    prior = tmp_path / ".releases" / "old" / "state.json"
    prior.parent.mkdir(parents=True)
    prior.write_text('{"sha":"' + ACTIVE_PIN_SHA + '","status":"failed"}')
    runtime.pin_file.write_text('{"sha":"' + ACTIVE_PIN_SHA + '","status":"active","state":"' + str(prior) + '"}')
    runtime.gate = lambda: pytest.fail("exchange gate must not run")
    with pytest.raises(ReleaseBlocked, match="completion evidence"):
        runtime.preflight()
    assert not runtime.release_dir.exists()


def test_bundle_rejects_mutable_image_before_release_directory(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / Path(MANIFEST).parent).mkdir(parents=True)
    (bundle / MANIFEST).write_text("{}")
    (bundle / "release-sha").write_text("a" * 40)
    with pytest.raises(ReleaseBlocked):
        Runtime(tmp_path, bundle, "a" * 40)


def test_workflow_routes_only_ai_tuner_image_to_scoped_release():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    assert 'if [ "$DEPLOY_MODE" = "ai-tuner-trial" ]; then' in workflow
    assert 'ai-tuner) echo "should_build=true"' in workflow
    assert "pattern: ai-tuner-trial-digest-*" in workflow
    assert "scripts/package_ai_tuner_trial.py" in workflow
    assert "ai-tuner-trial-${{ github.sha }}" in workflow


def test_external_dashboard_overlay_requires_completed_income_release():
    image = "ghcr.io/bearflower/trading-dashboard-api@sha256:" + "a" * 64
    state = {"sha": INCOME_RELEASE_SHA, "status": "active",
             "images": {"dashboard-api": image, "hrs": "unused"}}
    overlay = "services:\n  dashboard-api:\n    image: " + image + "\n    environment:\n      - LEDGER_DERIVED_ATTRIBUTION_ENABLED=1\n"
    assert validated_dashboard_image(state, overlay) == image
    with pytest.raises(ReleaseBlocked):
        validated_dashboard_image({**state, "status": "failed"}, overlay)
    with pytest.raises(ReleaseBlocked):
        validated_dashboard_image(state, overlay.replace(image, "latest"))
