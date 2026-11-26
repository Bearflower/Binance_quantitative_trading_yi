"""Release evidence must be complete, tied to one build, and contain no activation."""
import copy
from pathlib import Path

import pytest
import yaml

from scripts.full_regression_build import assemble, build_receipt, read_plan

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def receipts():
    return [build_receipt(image["name"], DIGEST, SHA, "123", "1") for image in read_plan()["images"]]


def test_release_covers_core_runtime_and_preserves_independent_experiments():
    manifest = assemble(receipts(), SHA, "123", "1")
    containers = [name for image in manifest["images"].values() for name in image["containers"]]
    assert len(manifest["images"]) == 11
    assert len(containers) == 23 and len(set(containers)) == 23
    assert {row["container"] for row in manifest["preserve_existing"]} == {
        "trading_system-hrs-research-observer", "trading_system-mtpcs-direction-shadow",
    }
    assert manifest["deployment_performed"] is False
    assert all(image["image"].endswith("@" + DIGEST) for image in manifest["images"].values())
    assert all(":latest" not in image["tag"] for image in manifest["images"].values())


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "sha", "run", "attempt", "image", "container", "platform"])
def test_incomplete_or_mixed_build_evidence_is_rejected(mutation):
    rows = receipts()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif mutation == "container":
        rows[0]["containers"].append("unreviewed-container")
    else:
        key = {"sha": "source_sha", "run": "run_id", "attempt": "run_attempt"}.get(mutation, mutation)
        rows[0][key] = "different"
    with pytest.raises(ValueError):
        assemble(rows, SHA, "123", "1")


@pytest.mark.parametrize("name,digest", [("unknown", DIGEST), ("grid", "latest"), ("grid", "sha256:" + "0" * 63)])
def test_unverified_digest_cannot_become_a_receipt(name, digest):
    with pytest.raises(ValueError):
        build_receipt(name, digest, SHA, "123", "1")


def test_workflow_only_builds_branch_digest_images_and_tests_without_runtime_mounts():
    source = (ROOT / ".github/workflows/full-regression-release.yml").read_text()
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["branches"] == ["codex/full-regression-production-20261126"]
    assert workflow["jobs"]["prepare"]["if"] == "github.ref == 'refs/heads/codex/full-regression-production-20261126'"
    assert set(workflow["jobs"]) == {"prepare", "build", "manifest"}
    steps = workflow["jobs"]["build"]["steps"]
    build = next(step for step in steps if step.get("id") == "image")
    assert build["with"]["context"] == "."
    assert "full-regression-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}" in build["with"]["tags"]
    probe = next(step["run"] for step in steps if step.get("name", "").startswith("Verify imports"))
    assert "--network none" in probe and "--read-only" in probe and "--entrypoint python" in probe
    assert "--tmpfs /app/logs:rw,nosuid,nodev,size=16m,mode=1777" in probe
    assert "--mount" not in probe and " -v " not in probe
    assert "ssh " not in source and "compose up" not in source and "secrets.SSH" not in source
    assert workflow["jobs"]["manifest"]["needs"] == ["prepare", "build"]


def test_tuner_image_contains_real_replay_dependencies_before_import_probe():
    source = (ROOT / "ai_tuner/Dockerfile").read_text()
    smoke = source.index('RUN python -c "import ai_tuner.main;')
    assert source.index("COPY strategies/hrs/ /app/strategies/hrs/") < smoke
    assert source.index("COPY strategies/btc_eth_aggressive/ /app/strategies/btc_eth_aggressive/") < smoke
    assert "import ai_tuner.replay.adapters.mtpcs_aggressive" in source


def test_release_artifact_includes_runner_verified_source_bundle():
    workflow = yaml.load((ROOT / '.github/workflows/full-regression-release.yml').read_text(), Loader=yaml.BaseLoader)
    commands = next(step['run'] for step in workflow['jobs']['manifest']['steps'] if 'run' in step)
    copies = commands.split('cp --parents', 1)[1]
    required = (
        'deployment/full-regression/build-plan.json',
        'deployment/full-regression/source-mount-removals.json',
        'scripts/deploy_full_regression.py', 'scripts/full_regression_build.py',
        'scripts/grid_release_guard.py', 'scripts/full_regression_live_gate.py',
        'scripts/database_permissions.py', 'database/postgres/permissions.json',
    )
    for relative in required:
        assert relative in copies
        assert (ROOT / relative).is_file()
