"""Deployment continuity guards without touching Docker or production."""

import subprocess

import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = (ROOT / "scripts" / "remote_deploy_runtime.sh").read_text()


def _preflight(action: str, identity: str) -> subprocess.CompletedProcess[str]:
    block = RUNTIME.split("stop_funding_collector_if_running() {", 1)[1].split(
        "# 1.5 磁盘预检查", 1
    )[0]
    script = f"""set -e
FUNDING_DEPLOY_ACTION={action}
docker() {{
  if [ "$1" = inspect ]; then
    printf '%s\\n' '{identity}'
  else
    echo UNEXPECTED_DOCKER_COMMAND >&2
    exit 50
  fi
}}
stop_funding_collector_if_running() {{{block}
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_keep_requires_healthy_owned_same_collector():
    good = "id1|2026-11-03T00:00:00Z|sha256:image|running|healthy|trading_system|ai-tuner-funding-schedule"
    assert _preflight("keep", good).returncode == 0
    for bad in (
        good.replace("healthy", "unhealthy"),
        good.replace("trading_system", "other_project"),
        good.replace("running", "exited"),
        good.replace("ai-tuner-funding-schedule", "ai-tuner"),
    ):
        assert _preflight("keep", bad).returncode != 0


def test_keep_and_rotate_compose_commands_are_disjoint_and_ordered():
    assert RUNTIME.index('if [ "$FUNDING_DEPLOY_ACTION" = "keep" ]; then') < RUNTIME.index(
        "docker compose pull\n"
    )
    normal_up = RUNTIME.split('echo "🔄 docker compose up -d..."', 1)[1].split(
        "# ========== step 4.5", 1
    )[0]
    assert 'if [ "$FUNDING_DEPLOY_ACTION" = "keep" ]; then' in normal_up
    assert '--parallel 1 up -d --no-deps "${KEEP_SERVICES[@]}"' in normal_up
    assert 'postgres|ai-tuner-funding-schedule) ;;' in normal_up
    assert "--parallel 1 up -d --remove-orphans" in normal_up
    after_gate = RUNTIME.split('if [ "$FUNDING_PERMISSION_BOUNDARY_OK" != "1" ]; then', 1)[1]
    assert 'if [ "$FUNDING_DEPLOY_ACTION" = "rotate" ]; then' in after_gate
    assert "FUNDING_IDENTITY_AFTER=$(funding_collector_identity)" in after_gate
    assert "\n  stop_funding_collector_if_running\n" in after_gate


def test_workflow_passes_validated_action_to_renderer():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "python3 scripts/select_funding_deploy.py" in workflow
    assert 'echo "funding_action=$FUNDING_ACTION" >> "$GITHUB_OUTPUT"' in workflow
    assert '--funding-action "${{ needs.detect.outputs.funding_action }}"' in workflow


@pytest.mark.parametrize("grid_enabled", [False, True])
def test_compose_update_excludes_database_and_collector_on_keep(tmp_path, grid_enabled):
    block = RUNTIME.split('echo "🔄 docker compose up -d..."', 1)[1].split(
        "# ========== step 4.5", 1
    )[0]
    (tmp_path / ".env").write_text(f"GRID_EXECUTOR_DEPLOY_ENABLED={str(grid_enabled).lower()}\n")
    stub = """docker() {
  case "$*" in
    *"config --services")
      printf '%s\\n' postgres ai-tuner-funding-schedule ai-tuner hrs-strategy
      if [[ "$*" == *"--profile grid-executor"* ]]; then echo grid-executor; fi ;;
    *) echo "CALL:$*" ;;
  esac
}
"""
    prefix = "CALL:compose " + ("--profile grid-executor " if grid_enabled else "")
    services = "ai-tuner hrs-strategy" + (" grid-executor" if grid_enabled else "")
    for action, args in (("keep", f"--no-deps {services}"), ("rotate", "--remove-orphans")):
        result = subprocess.run(
            ["bash", "-c", f"set -e\nFUNDING_DEPLOY_ACTION={action}\n" + stub + block],
            cwd=tmp_path, capture_output=True, text=True, check=True,
        )
        calls = [line for line in result.stdout.splitlines() if line.startswith("CALL:")]
        assert calls == [f"{prefix}--parallel 1 up -d {args}"]


def test_acl_failure_stops_exact_collector_and_postflight_rejects_change():
    acl = RUNTIME.split('if [ "$FUNDING_PERMISSION_BOUNDARY_OK" != "1" ]; then', 1)[1].split(
        "docker compose restart ai-tuner-snapshot", 1
    )[0]
    failed = subprocess.run(["bash", "-c", 'set -e\nFUNDING_PERMISSION_BOUNDARY_OK=0\n'
                             + 'stop_funding_collector_if_running() { echo STOP_EXACT_COLLECTOR; }\n'
                             + 'if [ "$FUNDING_PERMISSION_BOUNDARY_OK" != "1" ]; then' + acl],
                            capture_output=True, text=True)
    assert failed.returncode != 0
    assert "STOP_EXACT_COLLECTOR" in failed.stdout

    post = RUNTIME.split('if [ "$FUNDING_DEPLOY_ACTION" = "rotate" ]; then\n  docker compose --profile funding-schedule', 1)[1].split(
        'echo "✅ init-scripts 全部执行完毕"', 1
    )[0]
    changed = subprocess.run(["bash", "-c", 'set -e\nFUNDING_DEPLOY_ACTION=keep\n'
                              + 'FUNDING_IDENTITY_BEFORE=old\n'
                              + 'funding_collector_identity() { echo new; }\n'
                              + 'if [ "$FUNDING_DEPLOY_ACTION" = "rotate" ]; then\n'
                              + '  docker compose --profile funding-schedule' + post],
                             capture_output=True, text=True)
    assert changed.returncode != 0
    assert "身份或健康变化" in changed.stdout
