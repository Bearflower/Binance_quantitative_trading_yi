"""The allocation release must not replace unrelated production hotfixes."""

from pathlib import Path

from scripts.render_remote_deploy import render_remote_deploy


ROOT = Path(__file__).resolve().parents[2]


def test_reconcile_mode_is_renderable(tmp_path):
    output = tmp_path / "deploy.sh"
    render_remote_deploy(
        source=ROOT / "scripts/remote_deploy_runtime.sh",
        output=output,
        mode="allocation-reconcile",
        commit_sha="a" * 40,
        deploy_id="ABCDEF12",
    )
    assert 'DEPLOY_MODE="allocation-reconcile"' in output.read_text()


def test_reconcile_branch_updates_only_live_strategies_and_tuner():
    runtime = (ROOT / "scripts/remote_deploy_runtime.sh").read_text()
    isolated = runtime.split("# allocation-reconcile begin", 1)[1].split(
        "# allocation-reconcile end", 1
    )[0]
    assert "btc-eth-strategy" in isolated
    assert "btc-eth-aggressive-strategy" in isolated
    assert "new-coin-strategy" in isolated
    assert "hrs-strategy" in isolated
    assert "grid-strategy" in isolated
    assert "ai-tuner" in isolated
    assert "--remove-orphans" not in isolated
    assert "docker compose up -d --no-deps" in isolated
    assert "ROLLBACK_REQUIRED=1" in isolated
    assert "allocation_rollback_" in isolated
    assert "docker compose -f docker-compose.yml -f docker-compose.logging.yml" in isolated


def test_full_deploy_refuses_to_replace_unreconciled_hotfix_images():
    runtime = (ROOT / "scripts/remote_deploy_runtime.sh").read_text()
    assert 'if [ "$DEPLOY_MODE" = "full" ] && grep -q "allocation-fix:" docker-compose.override.yml' in runtime
