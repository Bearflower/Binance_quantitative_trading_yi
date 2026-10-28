import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICE = COMPOSE["services"]["mtcps-xau-calibrated-shadow"]
WORKFLOW = (ROOT / ".github/workflows/deploy.yml").read_text()
MODE_SELECTOR = (ROOT / "scripts/select_deploy_mode.py").read_text()
CALIBRATED_CONTAINER = "trading_system-mtcps-xau-calibrated-shadow"
CALIBRATED_SERVICE = "mtcps-xau-calibrated-shadow"
FIRST_CREATE_MARKER = (
    ROOT / ".github" / "deploy-modes" / "xau-calibrated-shadow-first-create"
)


def _env_map(service):
    return dict(item.split("=", 1) for item in service["environment"])


def test_calibrated_service_is_isolated_and_read_only():
    env = _env_map(SERVICE)
    assert SERVICE["container_name"] == "trading_system-mtcps-xau-calibrated-shadow"
    assert SERVICE["command"] == ["python", "-m", "strategies.btc_eth.shadow.main"]
    assert env["INSTANCE_ID"] == "mtcps_xau_calibrated_shadow"
    assert env["SHADOW_PROFILES"] == "xau_calibrated"
    assert env["SHADOW_RUN_ID_PREFIX"] == "xau-calibrated-shadow"
    assert env["SHADOW_CRON"] == "20 * * * *"
    assert env["EXECUTION_MODE"] == "shadow_batch"
    assert env["REAL_ORDERING_ENABLED"] == "false"
    assert env["STATE_REPORTING_ENABLED"] == "false"
    assert env["CAPITAL_STRATEGY_ID"] == ""
    assert env["CIRCUIT_BREAKER_POOL"] == ""


def test_calibrated_service_has_independent_runtime_paths():
    volumes = SERVICE["volumes"]
    assert "./logs/mtcps_xau_calibrated_shadow:/app/logs" in volumes
    assert "./data/mtcps_xau_calibrated_shadow:/app/data" in volumes
    assert all("strategies/btc_eth/shadow" not in volume for volume in volumes)


def _shell_tokens(block):
    return set(re.findall(r"[A-Za-z0-9_.-]+", block))


def _residual_cleanup_tokens():
    match = re.search(r'RESIDUAL_CANDIDATES="(?P<body>.*?)"', WORKFLOW, re.DOTALL)
    assert match is not None, "deploy.yml 缺少 RESIDUAL_CANDIDATES 清理清单"
    return _shell_tokens(match.group("body"))


def _long_running_health_gate_tokens():
    section = re.search(
        r"# 5\. 等全部长期运行容器就绪(?P<body>.*?)(?:# 6\.|\Z)",
        WORKFLOW,
        re.DOTALL,
    )
    assert section is not None, "deploy.yml 缺少长期运行容器就绪检查区段"
    loop = re.search(
        r"for\s+container\s+in\s+(?P<containers>.*?);\s*do",
        section.group("body"),
        re.DOTALL,
    )
    assert loop is not None, "长期运行容器就绪区段缺少 for container 清单"
    return _shell_tokens(loop.group("containers"))


def test_residual_cleanup_includes_calibrated_container():
    assert CALIBRATED_CONTAINER in _residual_cleanup_tokens()


def test_long_running_health_gate_includes_calibrated_container():
    assert CALIBRATED_CONTAINER in _long_running_health_gate_tokens()


def _calibrated_first_create_block():
    match = re.search(
        r"# calibrated-first-create begin(?P<body>.*?)"
        r"# calibrated-first-create end",
        WORKFLOW,
        re.DOTALL,
    )
    assert match is not None, "deploy.yml 缺少 calibrated 首次创建目标化分支"
    return match.group("body")


def _remote_script_through_targeted_exit():
    remote = re.search(
        r"cat > \"\$LOCAL_DEPLOY_SCRIPT\" << 'REMOTE_SCRIPT'(?P<body>.*?)"
        r"^\s*REMOTE_SCRIPT$",
        WORKFLOW,
        re.DOTALL | re.MULTILINE,
    )
    assert remote is not None, "deploy.yml 缺少远程部署脚本"
    prefix = remote.group("body").split("# calibrated-first-create end", 1)[0]
    assert 'exit 0' in prefix
    return prefix


def test_calibrated_healthcheck_uses_runtime_readiness_probe():
    assert SERVICE["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "strategies.btc_eth.shadow.readiness",
    ]


def test_first_create_mode_is_selected_by_one_time_changed_file_signal():
    assert FIRST_CREATE_MARKER.is_file()
    assert str(FIRST_CREATE_MARKER.relative_to(ROOT)) in MODE_SELECTOR
    assert 'return "calibrated-first-create"' in MODE_SELECTOR
    assert "scripts/select_deploy_mode.py" in WORKFLOW
    selection = re.search(
        r"- name: 🎯 选择部署模式(?P<body>.*?)(?=\n\s+- name:)",
        WORKFLOW,
        re.DOTALL,
    )
    assert selection is not None
    assert "docker inspect" not in selection.group("body")
    assert '--event-name "${{ github.event_name }}"' in selection.group("body")


def test_first_create_remote_path_targets_only_calibrated_service():
    block = _calibrated_first_create_block()

    _assert_targeted_block_safe(block)


def test_targeted_remote_prefix_has_no_full_deploy_side_effects():
    prefix = _remote_script_through_targeted_exit()

    for forbidden in (
        "docker system prune",
        "docker builder prune",
        "RESIDUAL_CANDIDATES",
        "docker compose run",
        "docker compose restart",
        "psql",
        "init-scripts",
    ):
        assert forbidden not in prefix
    assert "docker login ghcr.io" not in prefix
    _assert_targeted_block_safe(prefix)


def test_init_scripts_sync_is_full_deploy_only():
    sync_step = re.search(
        r"- name: 📦 同步数据库 init-scripts(?P<body>.*?)(?=\n\s+- name:)",
        WORKFLOW,
        re.DOTALL,
    )
    assert sync_step is not None
    assert "if: steps.deployment_mode.outputs.mode == 'full'" in sync_step.group("body")


def _workflow_step(name):
    match = re.search(
        rf"- name: {re.escape(name)}(?P<body>.*?)(?=\n\s+- name:|\Z)",
        WORKFLOW,
        re.DOTALL,
    )
    assert match is not None, f"deploy.yml 缺少步骤: {name}"
    return match.group("body")


def test_ghcr_token_is_streamed_to_independent_login_and_never_in_script():
    assert "__GHCR_TOKEN__" not in WORKFLOW
    assert 'sed -i "s|__GHCR_TOKEN__' not in WORKFLOW
    login = _workflow_step("🔐 远端登录 GHCR")
    assert "GHCR_PULL_TOKEN: ${{ secrets.GHCR_PULL_TOKEN }}" in login
    assert 'printf \'%s\' "$GHCR_PULL_TOKEN" | ssh ' in login
    assert "set -o pipefail" in login
    assert "docker login ghcr.io -u bearflower --password-stdin" in login
    assert "for i in 1 2 3" in login


def test_remote_deploy_script_path_is_unique_private_and_not_tmp():
    assert "/tmp/deploy.sh" not in WORKFLOW
    expected = (
        "/root/trading_system/.deploy_tmp/"
        "deploy-${{ github.run_id }}-${{ github.run_attempt }}.sh"
    )
    assert expected in WORKFLOW
    upload = _workflow_step("🚀 生成并上传远程部署脚本")
    assert 'mkdir -p /root/trading_system/.deploy_tmp' in upload
    assert 'chmod 700 /root/trading_system/.deploy_tmp' in upload
    assert 'chmod 700 \\"$REMOTE_DEPLOY_SCRIPT\\"' in upload


def _assert_remote_execution_always_cleans(step):
    trap_at = step.index("trap cleanup_deploy_script EXIT")
    execute_at = step.index('bash \\\"\\$REMOTE_DEPLOY_SCRIPT\\\"')
    assert trap_at < execute_at
    assert 'rm -f \\\"\\$REMOTE_DEPLOY_SCRIPT\\\"' in step
    assert 'rmdir /root/trading_system/.deploy_tmp' in step
    assert 'exit \\\"\\$rc\\\"' in step


def test_remote_execution_cleanup_preserves_success_or_failure_exit_code():
    step = _workflow_step("▶️ 执行远程部署脚本")
    _assert_remote_execution_always_cleans(step)
    assert "retry_connect" not in step
    assert "ssh 43.156.242.184" in step


def test_remote_execution_guard_detects_missing_failure_cleanup():
    step = _workflow_step("▶️ 执行远程部署脚本")
    with pytest.raises((AssertionError, ValueError)):
        _assert_remote_execution_always_cleans(
            step.replace("trap cleanup_deploy_script EXIT", "# trap removed")
        )


def _assert_targeted_block_safe(block):
    pull_commands = [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith("docker compose pull")
    ]
    up_commands = [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith("docker compose up")
    ]
    assert pull_commands == [f"docker compose pull {CALIBRATED_SERVICE}"]
    assert up_commands == [
        f"docker compose up -d --no-deps {CALIBRATED_SERVICE}"
    ]
    assert "btc-eth-strategy" not in block
    assert "mtcps-xau-shadow" not in block
    assert "--remove-orphans" not in block
    assert "docker compose run" not in block
    assert "docker compose restart" not in block
    assert "psql" not in block
    assert f"docker compose stop {CALIBRATED_SERVICE}" in block


@pytest.mark.parametrize(
    "mutate",
    [
        lambda block: block.replace(
            f"docker compose pull {CALIBRATED_SERVICE}",
            "docker compose pull",
        ),
        lambda block: block.replace("--no-deps ", ""),
        lambda block: block.replace(
            f"docker compose up -d --no-deps {CALIBRATED_SERVICE}",
            "docker compose up -d --remove-orphans",
        ),
    ],
)
def test_targeted_deploy_guard_detects_dangerous_command_mutations(mutate):
    with pytest.raises(AssertionError):
        _assert_targeted_block_safe(mutate(_calibrated_first_create_block()))


def test_ordinary_deploy_keeps_full_compose_path_outside_first_create_block():
    ordinary = WORKFLOW.replace(_calibrated_first_create_block(), "")

    assert "docker compose pull\n" in ordinary
    assert "docker compose up -d --remove-orphans" in ordinary
