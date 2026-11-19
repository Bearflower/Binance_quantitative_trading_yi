"""The remote shadow audit must remain a manually dispatched read-only job."""

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

import yaml

from strategies.grid.realtime.queue_audit import QueueAudit
from strategies.grid.realtime.reference_store import connect, ensure_schema


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/grid-shadow-audit.yml"
DEPLOY_WORKFLOW = WORKFLOW.with_name("deploy.yml")


def test_shadow_audit_workflow_never_runs_on_push_or_deploys_services():
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    source = WORKFLOW.read_text()
    for forbidden in ("docker compose", "docker-compose", "docker pull", "docker run",
                      "scripts/remote_deploy_runtime.sh", "ssh-keyscan"):
        assert forbidden not in source
    assert "StrictHostKeyChecking yes" in source
    assert "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5" in source


def test_shadow_audit_window_emits_two_valid_action_outputs(tmp_path):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    script = workflow["jobs"]["audit"]["steps"][1]["run"]
    output = tmp_path / "github-output"
    result = subprocess.run(
        ["bash", "-e"], input=script, text=True, capture_output=True,
        env={**os.environ, "AUDIT_DAYS": "1", "GITHUB_OUTPUT": str(output)},
    )

    assert result.returncode == 0, result.stderr
    lines = output.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("start_ms=")
    assert lines[1].startswith("end_ms=")
    assert int(lines[1].split("=", 1)[1]) - int(lines[0].split("=", 1)[1]) == 86400000


def test_shadow_audit_remote_step_has_valid_shell_structure():
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    script = workflow["jobs"]["audit"]["steps"][2]["run"]
    result = subprocess.run(["bash", "-n"], input=script, text=True,
                            capture_output=True)

    assert result.returncode == 0, result.stderr
    assert "root@43.156.242.184 +" not in script


def test_shadow_audit_changes_do_not_trigger_generic_trading_deployment():
    workflow = yaml.load(DEPLOY_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    ignored = set(workflow["on"]["push"]["paths-ignore"])

    assert "scripts/audit_grid_shadow_window.py" in ignored
    assert ".github/workflows/grid-shadow-audit.yml" in ignored


def test_two_file_remote_package_runs_without_repository_checkout(tmp_path):
    root = WORKFLOW.parents[2]
    package = tmp_path / "remote"
    for source in ("scripts/audit_grid_shadow_window.py",
                   "strategies/grid/realtime/queue_audit.py"):
        target = package / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / source, target)
    db_path = tmp_path / "grid.sqlite3"
    conn = connect(str(db_path), 1000)
    ensure_schema(conn)
    audit = QueueAudit(conn, "one", 1000)
    for at in range(1000, 5001, 1000):
        audit.checkpoint(at, 0, reference_status="VALID", feed_healthy=True,
                         storage_degraded=False)
    conn.close()

    result = subprocess.run([
        sys.executable, str(package / "scripts/audit_grid_shadow_window.py"),
        "--grid-db", str(db_path), "--start-ms", "1000", "--end-ms", "5000",
    ], cwd=tmp_path, env={**os.environ, "PYTHONPATH": ""}, text=True,
        capture_output=True)

    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report["reference_target_met"] is True
    assert report["market"] is None
    assert report["formal_m3_eligible"] is False
