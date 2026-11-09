from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.render_remote_deploy import RenderError, _validate
from scripts.scoped_stop_followup import Runtime, require_flat_release_gate, validate_active_state
from scripts.select_deploy_mode import DeployModeError, select_deploy_mode
from scripts.stop_followup_manifest import (
    ACTIVE_PIN_SHA, BASE, IMAGES, MANIFEST, MARKER, PIN_OVERLAY_SHA256,
    RELEASE, SOURCE, ReleaseBlocked, sha256, validate_manifest, validate_tree,
)


def _manifest():
    return {
        "schema_version": 1, "release_id": RELEASE, "base_revision": BASE,
        "active_pin_sha": ACTIVE_PIN_SHA,
        "expected_pin_overlay_sha256": PIN_OVERLAY_SHA256,
        "allowed_changes": [{"status": "A", "path": MARKER},
                            {"status": "A", "path": MANIFEST}],
        "source_checksums": {path: sha256(path.encode()) for path in SOURCE},
    }


def test_stop_release_selects_only_new_marker_and_one_image():
    assert IMAGES == {"btc-eth-aggr": "btc-eth-aggressive-strategy"}
    assert select_deploy_mode("push", BASE, [("A", MARKER)]) == "strategy-stop-followup"
    for changes in ([("M", MARKER)], [("A", MARKER), ("A", ".github/deploy-modes/strategy-code-followup-20261109-retry-1")]):
        with pytest.raises(DeployModeError):
            select_deploy_mode("push", BASE, changes)


def test_stop_release_manifest_rejects_extra_path_or_pin_drift():
    value = _manifest()
    changes = [("A", MARKER), ("A", MANIFEST)]
    read = lambda path: (MANIFEST if path == MARKER else path).encode()
    validate_tree(value, changes, read)
    with pytest.raises(ReleaseBlocked, match="outside reviewed"):
        validate_tree(value, changes + [("M", "docker-compose.yml")], read)
    value["active_pin_sha"] = "0" * 40
    with pytest.raises(ReleaseBlocked, match="pin"):
        validate_manifest(value)


def test_stop_release_requires_current_pin_and_exchange_proof():
    pin = {"sha": ACTIVE_PIN_SHA, "status": "active"}
    state = {"sha": ACTIVE_PIN_SHA, "status": "complete"}
    validate_active_state(pin, state, {"ok": True, "read_only": True})
    with pytest.raises(ReleaseBlocked):
        validate_active_state(dict(pin, status="activating"), state,
                              {"ok": True, "read_only": True})


def test_stop_release_uses_revision_two_and_preserves_funding():
    _validate("strategy-stop-followup", "a" * 40, "12345678", "keep")
    with pytest.raises(RenderError):
        _validate("strategy-stop-followup", "a" * 40, "12345678", "rotate")
    runtime = Runtime.__new__(Runtime)
    calls = []
    runtime.run = lambda argv, **kwargs: calls.append(argv) or '{"ok":true,"read_only":true}'
    runtime.gate()
    assert calls[0][calls[0].index("--revision") + 1] == "2"


def test_stop_release_refuses_to_restart_aggressive_with_live_position():
    require_flat_release_gate({"position_exposure": [], "protected_positions": 0})
    with pytest.raises(ReleaseBlocked, match="flat account"):
        require_flat_release_gate({"position_exposure": [{"symbol": "SOLUSDT"}],
                                   "protected_positions": 1})


def test_schema_stage_backups_before_atomic_migration(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    runtime.release_dir = tmp_path / "release"
    runtime.release_dir.mkdir()
    runtime.bundle = tmp_path / "bundle"
    migration = runtime.bundle / "database/postgres/init-scripts/16-aggressive-stop-bindings.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("CREATE TABLE example(id int);", encoding="utf-8")
    calls = []

    def fake_dump(argv, **kwargs):
        calls.append("backup")
        kwargs["stdout"].write(b"backup")
        return SimpleNamespace(returncode=0)

    def fake_run(argv, **kwargs):
        if kwargs.get("input"):
            calls.append("migration")
        elif "IS NULL" in argv[-1]:
            calls.append("absent")
        else:
            calls.append("verify")
        return "t"

    runtime.run = fake_run
    runtime.record = lambda *args: calls.append("record")
    with patch("scripts.scoped_stop_followup.subprocess.run", side_effect=fake_dump):
        runtime.apply_schema()
    assert calls == ["absent", "backup", "migration", "verify", "record"]
    assert (runtime.release_dir / "database-before.dump").read_bytes() == b"backup"


def test_schema_stage_rejects_preexisting_table_before_backup(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    runtime.release_dir = tmp_path
    runtime.run = lambda *args, **kwargs: "f"
    with pytest.raises(ReleaseBlocked, match="already exist"):
        runtime.apply_schema()
    assert not (tmp_path / "database-before.dump").exists()
