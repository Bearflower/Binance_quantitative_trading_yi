from pathlib import Path

from scripts.render_remote_deploy import render_remote_deploy


ROOT = Path(__file__).resolve().parents[2]


def test_execution_facts_first_create_renders_isolated_deploy(tmp_path):
    output = tmp_path / "deploy.sh"
    render_remote_deploy(
        source=ROOT / "scripts/remote_deploy_runtime.sh",
        output=output,
        mode="execution-facts-first-create",
        commit_sha="a" * 40,
        deploy_id="00000101",
    )
    runtime = output.read_text()
    isolated = runtime.split("# execution-facts-first-create begin", 1)[1].split(
        "# execution-facts-first-create end", 1
    )[0]
    assert "docker compose pull hrs-strategy" in isolated
    assert "docker compose up -d --no-deps execution-facts-collector" in isolated
    assert "exit 0" in isolated
    assert "--remove-orphans" not in isolated


def test_execution_facts_mode_builds_only_hrs():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    mode = workflow.split('if [ "$DEPLOY_MODE" = "execution-facts-first-create" ]; then', 1)[1]
    decision = mode.split("# 仅变更分批日志", 1)[0]
    assert 'if [ "$SERVICE_NAME" = "hrs" ]; then' in decision
    assert 'echo "should_build=true" >> "$GITHUB_OUTPUT"' in decision
    assert 'echo "should_build=false" >> "$GITHUB_OUTPUT"' in decision


def test_collector_failure_exits_for_restart():
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["execution-facts-collector"]
    assert service["restart"] == "unless-stopped"
    assert service["command"][-1].endswith("done; exit 1")
